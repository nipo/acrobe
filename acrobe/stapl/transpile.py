"""STAPL to Python transpiler.

Converts a parsed STAPL program into a standalone Python script that
executes the same JTAG operations using acrobe's TAP API.

The output separates code (procedures) from data (large boolean literals),
externalizing big arrays as binary files and emitting readable Python
with recovered control flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .parser import (
    Program, ActionDef, ProcedureDef, DataBlock,
    IntegerDecl, BooleanDecl, BooleanLiteral,
    AssignStmt, GotoStmt, CallStmt, ExitStmt, IfStmt,
    ForStmt, NextStmt, PushStmt, PopStmt, ReturnStmt,
    DrScanStmt, IrScanStmt, DrStopStmt, IrStopStmt,
    StateStmt, WaitStmt, TrstStmt, FrequencyStmt,
    PreDrStmt, PostDrStmt, PreIrStmt, PostIrStmt,
    PrintStmt, ExportStmt, VmapStmt, VectorStmt,
    Expr, IntLiteral, VarRef, ArrayIndex, ArraySubrange,
    ArrayWhole, UnaryOp, BinOp, FuncCall,
)


# ============================================================
# Configuration
# ============================================================

@dataclass
class TranspileConfig:
    data_threshold: int = 256  # bits; boolean arrays >= this → binary files


# ============================================================
# Variable information
# ============================================================

@dataclass
class VarInfo:
    name: str
    vtype: str  # 'integer' | 'boolean'
    is_array: bool
    size: int | None  # bit count for boolean, element count for integer
    has_init: bool
    init_data: bytes | None  # raw bytes for boolean with literal init
    init_bit_count: int | None
    scope: str  # 'data' | 'local'
    data_block: str | None  # DATA block name, if scope == 'data'


# ============================================================
# FOR/NEXT region extraction
# ============================================================

@dataclass
class ForRegion:
    """A FOR/NEXT structured region extracted from flat statement list."""
    for_stmt: ForStmt
    body: list  # may contain nested ForRegions and other stmts
    next_stmt: NextStmt


def _find_matching_next(stmts, for_idx):
    """Find the matching NEXT for a FOR at for_idx (by nesting depth)."""
    depth = 1
    for j in range(for_idx + 1, len(stmts)):
        if isinstance(stmts[j], ForStmt):
            depth += 1
        elif isinstance(stmts[j], NextStmt):
            depth -= 1
            if depth == 0:
                return j
    assert False, f"No matching NEXT for FOR at index {for_idx}"


def _extract_for_regions(stmts):
    """Replace FOR/NEXT pairs with ForRegion nodes. Returns processed list."""
    result = []
    i = 0
    while i < len(stmts):
        if isinstance(stmts[i], ForStmt):
            end = _find_matching_next(stmts, i)
            body = _extract_for_regions(stmts[i + 1:end])
            result.append(ForRegion(stmts[i], body, stmts[end]))
            i = end + 1
        else:
            result.append(stmts[i])
            i += 1
    return result


def _preprocess_for_next(stmts, labels):
    """Extract FOR/NEXT regions and adjust label indices.

    Returns (processed_stmts, adjusted_labels).
    FOR/NEXT pairs that contain labels are left as raw statements
    (the dispatch will handle them). Inner FOR/NEXT pairs that don't
    contain labels are still extracted even if their outer FOR does.
    """
    # Find ALL FOR/NEXT pairs at every nesting level using a stack
    all_pairs = {}  # for_idx → next_idx
    stack = []
    for i, stmt in enumerate(stmts):
        if isinstance(stmt, ForStmt):
            stack.append(i)
        elif isinstance(stmt, NextStmt):
            if stack:
                for_idx = stack.pop()
                all_pairs[for_idx] = i

    # Remove pairs that DIRECTLY contain labels
    # (a label at index L is "inside" FOR[f]..NEXT[n] if f < L <= n)
    label_indices = set(labels.values())
    tainted = set()
    for f_start, f_end in all_pairs.items():
        for idx in label_indices:
            if f_start < idx <= f_end:
                tainted.add(f_start)
                break
    for t in tainted:
        del all_pairs[t]

    # Only keep outermost non-tainted pairs for top-level extraction.
    # Inner pairs will be extracted recursively by _extract_for_regions.
    outermost = {}
    sorted_pairs = sorted(all_pairs.items())
    skip_until = -1
    for f_start, f_end in sorted_pairs:
        if f_start > skip_until:
            outermost[f_start] = f_end
            skip_until = f_end

    # Build processed list with old→new index mapping
    processed = []
    old_to_new = {}
    i = 0
    while i < len(stmts):
        old_to_new[i] = len(processed)
        if i in outermost:
            next_idx = outermost[i]
            body = _extract_for_regions(stmts[i + 1:next_idx])
            processed.append(ForRegion(stmts[i], body, stmts[next_idx]))
            for j in range(i + 1, next_idx + 1):
                old_to_new[j] = len(processed) - 1
            i = next_idx + 1
        else:
            processed.append(stmts[i])
            i += 1

    # Also map index == len(stmts) for labels pointing past end
    old_to_new[len(stmts)] = len(processed)

    adjusted = {}
    for label, idx in labels.items():
        adjusted[label] = old_to_new[idx]

    return processed, adjusted


# ============================================================
# Basic block construction
# ============================================================

@dataclass
class Fallthrough:
    pass


@dataclass
class Jump:
    target: str


@dataclass
class CondJump:
    condition: Expr
    target: str  # taken when condition is true


@dataclass
class BlockExit:
    code: Expr


@dataclass
class BlockReturn:
    pass


@dataclass
class Block:
    name: str
    index: int  # position in source order
    stmts: list  # non-terminal statements (may include ForRegion)
    terminal: Fallthrough | Jump | CondJump | BlockExit | BlockReturn


def _build_blocks(stmts, labels):
    """Build basic blocks from preprocessed statement list.

    Args:
        stmts: list of statements and ForRegions (FOR/NEXT already extracted)
        labels: dict[str, int] adjusted label → index mapping

    Returns:
        (blocks, name_to_idx) where blocks is a list of Block and
        name_to_idx maps block names to list indices.
    """
    # Reverse label map: index → label name (first label wins)
    idx_to_label = {}
    for label, idx in labels.items():
        if idx not in idx_to_label:
            idx_to_label[idx] = label

    # Find all block boundary indices
    boundaries = {0}
    for _, idx in labels.items():
        boundaries.add(idx)

    for i, stmt in enumerate(stmts):
        if isinstance(stmt, GotoStmt):
            boundaries.add(i + 1)
            target = stmt.label.upper()
            if target in labels:
                boundaries.add(labels[target])
        elif isinstance(stmt, IfStmt) and isinstance(stmt.then_stmt, GotoStmt):
            boundaries.add(i + 1)
            target = stmt.then_stmt.label.upper()
            if target in labels:
                boundaries.add(labels[target])
        elif isinstance(stmt, ExitStmt):
            boundaries.add(i + 1)

    # Sort and filter valid boundaries
    boundaries = sorted(b for b in boundaries if 0 <= b <= len(stmts))
    if not boundaries or boundaries[0] != 0:
        boundaries = [0] + boundaries

    # Create blocks
    blocks = []
    for bi in range(len(boundaries)):
        start = boundaries[bi]
        end = boundaries[bi + 1] if bi + 1 < len(boundaries) else len(stmts)

        if start >= len(stmts):
            # Label at the very end of the procedure (e.g. before ENDPROC).
            # Still create an empty block so GOTOs to it work.
            name = idx_to_label.get(start, f'_block_{start}')
            blocks.append(Block(name, bi, [], Fallthrough()))
            break

        name = idx_to_label.get(start, f'_block_{start}')
        block_stmts = []
        terminal = Fallthrough()

        for j in range(start, end):
            s = stmts[j]

            if isinstance(s, GotoStmt):
                terminal = Jump(s.label.upper())
                break
            elif isinstance(s, IfStmt) and isinstance(s.then_stmt, GotoStmt):
                terminal = CondJump(s.condition, s.then_stmt.label.upper())
                break
            elif isinstance(s, ExitStmt):
                terminal = BlockExit(s.code)
                break
            else:
                block_stmts.append(s)

        blocks.append(Block(name, bi, block_stmts, terminal))

    # Append implicit return if last block falls through
    if blocks and isinstance(blocks[-1].terminal, Fallthrough):
        blocks[-1].terminal = BlockReturn()

    name_to_idx = {b.name: i for i, b in enumerate(blocks)}
    return blocks, name_to_idx


# ============================================================
# Control flow recovery
# ============================================================

# Structured IR nodes

@dataclass
class SStmt:
    """A plain statement (or ForRegion)."""
    stmt: object


@dataclass
class SFor:
    """Python for loop."""
    var: str
    start: Expr
    end: Expr
    step: Expr | None
    body: list  # of structured nodes


@dataclass
class SWhileTrue:
    """while True loop with breaks inside."""
    body: list


@dataclass
class SWhile:
    """while condition loop."""
    condition: Expr
    body: list


@dataclass
class SIf:
    """if/elif/else."""
    condition: Expr
    then_body: list
    else_body: list | None


@dataclass
class SBreak:
    pass


@dataclass
class SContinue:
    pass


@dataclass
class SExit:
    code: Expr


@dataclass
class SReturn:
    pass


@dataclass
class SDispatch:
    """Fallback: block dispatch for unrecoverable control flow."""
    entry: str
    blocks: list[Block]
    name_to_idx: dict[str, int]


def _negate_expr(expr: Expr) -> Expr:
    """Negate an expression. Simplify double negation and comparisons."""
    if isinstance(expr, UnaryOp) and expr.op == '!':
        return expr.operand
    _flip = {'==': '!=', '!=': '==', '<': '>=', '>': '<=', '<=': '>', '>=': '<'}
    if isinstance(expr, BinOp) and expr.op in _flip:
        return BinOp(_flip[expr.op], expr.left, expr.right)
    return UnaryOp('!', expr)


def _has_unresolved_gotos(nodes):
    """Check if any structured IR nodes contain unresolved GotoStmt."""
    for node in nodes:
        match node:
            case SWhileTrue(body=body):
                if _has_unresolved_gotos(body):
                    return True
            case SWhile(body=body):
                if _has_unresolved_gotos(body):
                    return True
            case SIf(then_body=body, else_body=eb):
                if _has_unresolved_gotos(body):
                    return True
                if eb and _has_unresolved_gotos(eb):
                    return True
            case SFor(body=body):
                if _has_unresolved_gotos(body):
                    return True
            case SStmt(stmt=s):
                if isinstance(s, GotoStmt):
                    return True
                if isinstance(s, IfStmt) and isinstance(s.then_stmt, GotoStmt):
                    return True
    return False


def _recover_control_flow(blocks, name_to_idx):
    """Attempt to recover structured control flow from basic blocks.

    Returns a list of structured IR nodes. Falls back to SDispatch
    if the control flow cannot be fully recovered, or if unresolved
    GOTO statements remain after structuring.
    """
    if not blocks:
        return []

    try:
        result = _structure_region(blocks, 0, len(blocks), name_to_idx)
        # Check for leftover GOTOs that the structurer couldn't handle
        if _has_unresolved_gotos(result):
            return [SDispatch(blocks[0].name, blocks, name_to_idx)]
        return result
    except _UnstructurableError:
        return [SDispatch(blocks[0].name, blocks, name_to_idx)]


class _UnstructurableError(Exception):
    pass


def _structure_region(blocks, start, end, name_to_idx):
    """Recursively structure blocks[start:end] into structured IR.

    Raises _UnstructurableError if a goto target falls outside the
    region or can't be matched to a known pattern.
    """
    if start >= end:
        return []

    # Find back-edges (loops) within this region
    # back-edge: block i has a jump to block j where j >= start and j <= i
    loops = {}  # header_block_idx → tail_block_idx
    for i in range(start, end):
        block = blocks[i]
        targets = _get_jump_targets(block, name_to_idx)
        for t in targets:
            if start <= t <= i:
                # Back-edge from block i to block t
                if t not in loops or i > loops[t]:
                    loops[t] = i

    result = []
    i = start
    while i < end:
        # Check if we're at a loop header
        if i in loops:
            tail = loops[i]
            loop_node = _structure_loop(blocks, i, tail, name_to_idx)
            result.append(loop_node)
            i = tail + 1
            continue

        block = blocks[i]

        # Emit block's non-terminal statements
        for stmt in block.stmts:
            result.append(_wrap_stmt(stmt))

        # Handle terminal
        if isinstance(block.terminal, CondJump):
            target_idx = name_to_idx.get(block.terminal.target)
            if target_idx is None or target_idx < start or target_idx > end:
                raise _UnstructurableError(
                    f"CondJump target {block.terminal.target} out of region [{start}:{end}]")

            if target_idx <= i:
                # Back-edge handled above, shouldn't reach here
                raise _UnstructurableError("Unexpected back-edge in linear scan")

            # Forward conditional jump: if/else pattern
            if_node = _try_structure_if(blocks, i, target_idx, end, name_to_idx)
            if if_node is not None:
                result.append(if_node[0])
                i = if_node[1]
                continue
            else:
                raise _UnstructurableError(
                    f"Cannot structure forward jump from block {i} to {target_idx}")

        elif isinstance(block.terminal, Jump):
            target_idx = name_to_idx.get(block.terminal.target)
            if target_idx is None:
                raise _UnstructurableError(
                    f"Jump target {block.terminal.target} not found")
            if target_idx == i + 1:
                pass  # redundant goto to next block, ignore
            elif target_idx > i:
                # Forward unconditional jump — could be part of if/else (skip else)
                # or a break. Hard to handle in isolation.
                raise _UnstructurableError(
                    f"Standalone forward jump from block {i} to {target_idx}")
            else:
                # Back-edge: should have been caught as loop
                raise _UnstructurableError(
                    f"Standalone back-jump from block {i} to {target_idx}")

        elif isinstance(block.terminal, BlockExit):
            result.append(SExit(block.terminal.code))

        elif isinstance(block.terminal, BlockReturn):
            # Only emit explicit return if it's not the last block
            if i < end - 1:
                result.append(SReturn())

        i += 1

    return result


def _structure_loop(blocks, header_idx, tail_idx, name_to_idx):
    """Structure a loop from blocks[header_idx] through blocks[tail_idx].

    The back-edge is at blocks[tail_idx] jumping back to blocks[header_idx].
    """
    header = blocks[header_idx]
    tail = blocks[tail_idx]

    # Determine loop type based on where the condition is

    # Case 1: Condition at tail (bottom-tested loop)
    # while True: body; if not cond: break
    if isinstance(tail.terminal, CondJump):
        target_idx = name_to_idx.get(tail.terminal.target)
        if target_idx == header_idx:
            # Back-edge is the conditional: loop back if condition true
            # → while True: body; if not cond: break
            body = _structure_loop_body(blocks, header_idx, tail_idx, name_to_idx)
            # Add tail's statements
            for stmt in tail.stmts:
                body.append(_wrap_stmt(stmt))
            # Add break condition (loop continues if condition true, break if false)
            body.append(SIf(_negate_expr(tail.terminal.condition), [SBreak()], None))
            return SWhileTrue(body)

    # Case 2: Condition at header (top-tested loop)
    if isinstance(header.terminal, CondJump):
        exit_target = name_to_idx.get(header.terminal.target)
        if exit_target is not None and exit_target == tail_idx + 1:
            # Header jumps past the loop when condition is true → while not cond
            if not header.stmts:
                # Clean while loop: no statements before condition
                body = _structure_loop_body(
                    blocks, header_idx + 1, tail_idx, name_to_idx)
                # Add tail's statements to body
                for stmt in blocks[tail_idx].stmts:
                    body.append(_wrap_stmt(stmt))
                return SWhile(_negate_expr(header.terminal.condition), body)
            else:
                # Header has statements before the condition
                body = []
                for stmt in header.stmts:
                    body.append(_wrap_stmt(stmt))
                body.append(SIf(header.terminal.condition, [SBreak()], None))
                body.extend(
                    _structure_loop_body(
                        blocks, header_idx + 1, tail_idx, name_to_idx))
                for stmt in blocks[tail_idx].stmts:
                    body.append(_wrap_stmt(stmt))
                return SWhileTrue(body)

    # Case 3: Unconditional back-edge at tail (while True, break inside)
    if isinstance(tail.terminal, Jump):
        target_idx = name_to_idx.get(tail.terminal.target)
        if target_idx == header_idx:
            body = _structure_loop_body(blocks, header_idx, tail_idx, name_to_idx)
            for stmt in tail.stmts:
                body.append(_wrap_stmt(stmt))
            return SWhileTrue(body)

    # Fallback: while True with entire range as body
    body = []
    for bi in range(header_idx, tail_idx + 1):
        for stmt in blocks[bi].stmts:
            body.append(_wrap_stmt(stmt))
        t = blocks[bi].terminal
        if isinstance(t, CondJump):
            target_idx = name_to_idx.get(t.target)
            if target_idx == header_idx:
                body.append(SIf(_negate_expr(t.condition), [SBreak()], None))
            elif target_idx is not None and target_idx > tail_idx:
                body.append(SIf(t.condition, [SBreak()], None))
        elif isinstance(t, BlockExit):
            body.append(SExit(t.code))
    return SWhileTrue(body)


def _structure_loop_body(blocks, start, end, name_to_idx):
    """Structure the interior of a loop (blocks[start:end], exclusive of tail)."""
    if start >= end:
        return []
    try:
        return _structure_region(blocks, start, end, name_to_idx)
    except _UnstructurableError:
        # Fall back to emitting statements linearly
        body = []
        for bi in range(start, end):
            for stmt in blocks[bi].stmts:
                body.append(_wrap_stmt(stmt))
            t = blocks[bi].terminal
            if isinstance(t, CondJump):
                # Emit as if-break or if-continue
                target_idx = name_to_idx.get(t.target)
                if target_idx is not None and target_idx > end:
                    body.append(SIf(t.condition, [SBreak()], None))
                elif target_idx is not None and target_idx == start:
                    body.append(SIf(t.condition, [SContinue()], None))
                else:
                    body.append(SIf(t.condition,
                                    [SStmt(GotoStmt(t.target))], None))
            elif isinstance(t, Jump):
                target_idx = name_to_idx.get(t.target)
                if target_idx is not None and target_idx > end:
                    body.append(SBreak())
                elif target_idx == start:
                    body.append(SContinue())
            elif isinstance(t, BlockExit):
                body.append(SExit(t.code))
        return body


def _try_structure_if(blocks, cond_block_idx, target_idx, region_end, name_to_idx):
    """Try to structure an if/else pattern.

    cond_block_idx: block with the conditional forward jump
    target_idx: where the jump goes (else branch start or merge point)

    Returns (structured_node, next_idx) or None if pattern doesn't match.
    """
    cond_block = blocks[cond_block_idx]
    condition = cond_block.terminal.condition

    # Check for if-then-else: blocks between cond+1 and target, last one
    # has an unconditional jump past target (skipping else body)
    last_then_idx = target_idx - 1
    if last_then_idx > cond_block_idx:
        last_then = blocks[last_then_idx]
        if isinstance(last_then.terminal, Jump):
            else_end_idx = name_to_idx.get(last_then.terminal.target)
            if else_end_idx is not None and else_end_idx > target_idx and else_end_idx <= region_end:
                # if/else pattern detected
                try:
                    then_body = _structure_region(
                        blocks, cond_block_idx + 1, last_then_idx, name_to_idx)
                    # Add last_then's statements (without the jump)
                    for stmt in last_then.stmts:
                        then_body.append(_wrap_stmt(stmt))
                    else_body = _structure_region(
                        blocks, target_idx, else_end_idx, name_to_idx)
                    node = SIf(_negate_expr(condition), then_body, else_body)
                    return (node, else_end_idx)
                except _UnstructurableError:
                    pass

    # Simple if-then (no else): condition true jumps past the then body
    try:
        then_body = _structure_region(
            blocks, cond_block_idx + 1, target_idx, name_to_idx)
        node = SIf(_negate_expr(condition), then_body, None)
        return (node, target_idx)
    except _UnstructurableError:
        return None


def _get_jump_targets(block, name_to_idx):
    """Get the block indices of all jump targets from a block."""
    targets = []
    if isinstance(block.terminal, Jump):
        idx = name_to_idx.get(block.terminal.target)
        if idx is not None:
            targets.append(idx)
    elif isinstance(block.terminal, CondJump):
        idx = name_to_idx.get(block.terminal.target)
        if idx is not None:
            targets.append(idx)
    return targets


def _wrap_stmt(stmt):
    """Wrap a statement or ForRegion into a structured IR node."""
    if isinstance(stmt, ForRegion):
        body = [_wrap_stmt(s) for s in stmt.body]
        return SFor(stmt.for_stmt.var, stmt.for_stmt.start,
                     stmt.for_stmt.end, stmt.for_stmt.step, body)
    return SStmt(stmt)


# ============================================================
# Variable analysis
# ============================================================

def _analyze_variables(program, config):
    """Analyze all variables in the program.

    Returns:
        (var_infos, data_files) where:
        - var_infos: dict[str, VarInfo]
        - data_files: dict[str, bytes] (filename → binary data for large booleans)
    """
    var_infos = {}
    data_files = {}

    # Collect DATA block variables
    for block_name, data_block in program.data_blocks.items():
        for stmt in data_block.statements:
            vi = _analyze_decl(stmt, 'data', block_name)
            if vi:
                var_infos[vi.name] = vi
                # Check if this should be an external file
                if (vi.vtype == 'boolean' and vi.init_data is not None
                        and vi.init_bit_count is not None
                        and vi.init_bit_count >= config.data_threshold):
                    filename = f"{vi.name}.bin"
                    data_files[filename] = vi.init_data
                    vi.init_data = None  # mark as externalized

    # Collect procedure-local variables
    for proc_name, proc in program.procedures.items():
        for stmt in proc.statements:
            if isinstance(stmt, (IntegerDecl, BooleanDecl)):
                vi = _analyze_decl(stmt, 'local', None)
                if vi and vi.name not in var_infos:
                    var_infos[vi.name] = vi

    return var_infos, data_files


def _analyze_decl(stmt, scope, data_block):
    """Analyze a variable declaration statement."""
    if isinstance(stmt, IntegerDecl):
        is_array = stmt.size is not None
        size = None
        if is_array and isinstance(stmt.size, IntLiteral):
            size = stmt.size.value
        return VarInfo(
            name=stmt.name,
            vtype='integer',
            is_array=is_array,
            size=size,
            has_init=stmt.init is not None,
            init_data=None,
            init_bit_count=None,
            scope=scope,
            data_block=data_block,
        )
    elif isinstance(stmt, BooleanDecl):
        is_array = stmt.size is not None
        size = None
        if is_array and isinstance(stmt.size, IntLiteral):
            size = stmt.size.value
        init_data = None
        init_bit_count = None
        if stmt.init is not None and isinstance(stmt.init, BooleanLiteral):
            init_data = stmt.init.data
            init_bit_count = stmt.init.bit_count
        return VarInfo(
            name=stmt.name,
            vtype='boolean',
            is_array=is_array,
            size=size,
            has_init=stmt.init is not None,
            init_data=init_data,
            init_bit_count=init_bit_count,
            scope=scope,
            data_block=data_block,
        )
    return None


# ============================================================
# Python code writer
# ============================================================

class _Writer:
    """Indentation-aware code writer."""

    def __init__(self):
        self._lines = []
        self._indent = 0

    def line(self, text=''):
        if text:
            self._lines.append('    ' * self._indent + text)
        else:
            self._lines.append('')

    def indent(self):
        self._indent += 1

    def dedent(self):
        self._indent -= 1

    def text(self):
        return '\n'.join(self._lines) + '\n'


# ============================================================
# Expression emission
# ============================================================

class _ExprEmitter:
    """Emit Python expressions from STAPL AST expressions."""

    def __init__(self, var_infos, local_vars):
        self._var_infos = var_infos
        self._local_vars = local_vars  # set of names declared locally

    def _ref(self, name):
        """Variable reference: self.NAME for data, NAME for locals."""
        if name in self._local_vars:
            return name
        return f'self.{name}'

    def _is_boolean_array(self, name):
        vi = self._var_infos.get(name)
        return vi is not None and vi.vtype == 'boolean' and vi.is_array

    def int_expr(self, expr):
        """Emit expression that evaluates to int."""
        match expr:
            case IntLiteral(value=v):
                if abs(v) > 255:
                    return f'0x{v:X}' if v >= 0 else f'-0x{-v:X}'
                return str(v)

            case VarRef(name=n):
                return self._ref(n)

            case ArrayIndex(name=n, index=idx):
                arr = self._ref(n)
                idx_s = self.int_expr(idx)
                if self._is_boolean_array(n):
                    return f'{arr}.get_bit({idx_s})'
                return f'{arr}[{idx_s}]'

            case ArraySubrange(name=n, high=h, low=l):
                arr = self._ref(n)
                return f'{arr}.get_subrange({self.int_expr(h)}, {self.int_expr(l)}).to_int()'

            case ArrayWhole(name=n):
                arr = self._ref(n)
                if self._is_boolean_array(n):
                    return f'{arr}.to_int()'
                return arr

            case UnaryOp(op='!', operand=a):
                return f'(not {self.int_expr(a)})'

            case UnaryOp(op=op, operand=a):
                return f'({op}{self.int_expr(a)})'

            case BinOp(op='&&', left=a, right=b):
                return f'({self.int_expr(a)} and {self.int_expr(b)})'

            case BinOp(op='||', left=a, right=b):
                return f'({self.int_expr(a)} or {self.int_expr(b)})'

            case BinOp(op=op, left=a, right=b):
                return f'({self.int_expr(a)} {op} {self.int_expr(b)})'

            case FuncCall(name='ABS', arg=a):
                return f'abs({self.int_expr(a)})'

            case FuncCall(name='INT', arg=a):
                return self._int_of_bits(a)

            case FuncCall(name='CHR$', arg=a):
                return f'chr({self.int_expr(a)})'

            case FuncCall(name=n, arg=a):
                return f'{n}({self.int_expr(a)})'

            case BooleanLiteral(data=d, bit_count=bc):
                # Small boolean literal → int
                val = int.from_bytes(d, 'little')
                if val > 255:
                    return f'0x{val:X}'
                return str(val)

            case _:
                return repr(expr)

    def _int_of_bits(self, expr):
        """Emit int conversion of a bits expression."""
        match expr:
            case ArraySubrange(name=n, high=h, low=l):
                arr = self._ref(n)
                return f'{arr}.get_subrange({self.int_expr(h)}, {self.int_expr(l)}).to_int()'
            case ArrayWhole(name=n):
                return f'{self._ref(n)}.to_int()'
            case VarRef(name=n):
                if self._is_boolean_array(n):
                    return f'{self._ref(n)}.to_int()'
                return self._ref(n)
            case _:
                return f'{self.bits_expr(expr)}.to_int()'

    def bits_expr(self, expr):
        """Emit expression that evaluates to BitArray."""
        match expr:
            case VarRef(name=n):
                return self._ref(n)

            case ArraySubrange(name=n, high=h, low=l):
                arr = self._ref(n)
                return f'{arr}.get_subrange({self.int_expr(h)}, {self.int_expr(l)})'

            case ArrayWhole(name=n):
                return self._ref(n)

            case BooleanLiteral(data=d, bit_count=bc):
                return f'BitArray({bc}, {d!r})'

            case FuncCall(name='BOOL', arg=a):
                return f'BitArray.from_int({self.int_expr(a)})'

            case _:
                # Fallback: convert int expression to BitArray
                return f'BitArray.from_int({self.int_expr(expr)})'

    def scan_tdi_expr(self, expr):
        """Emit expression for scan TDI data (needs to produce bytes)."""
        return f'{self.bits_expr(expr)}.to_bytes()'

    def scan_tdi_as_int(self, expr):
        """Emit expression for IR scan (integer value)."""
        match expr:
            case BooleanLiteral(data=d, bit_count=bc):
                val = int.from_bytes(d, 'little') & ((1 << bc) - 1)
                if val > 255:
                    return f'0x{val:X}'
                return str(val)
            case _:
                return self._int_of_bits(expr)


# ============================================================
# Statement emission
# ============================================================

class _StmtEmitter:
    """Emit Python statements from structured IR."""

    def __init__(self, w, expr_em, var_infos, proc_locals, program):
        self._w = w
        self._expr = expr_em
        self._var_infos = var_infos
        self._proc_locals = proc_locals
        self._program = program
        self._in_dispatch = False  # True when inside _emit_dispatch

    def emit_structured(self, nodes):
        """Emit a list of structured IR nodes."""
        for node in nodes:
            self._emit_node(node)

    def _emit_node(self, node):
        w = self._w
        match node:
            case SStmt(stmt=stmt):
                self._emit_plain_stmt(stmt)

            case SFor(var=var, start=start, end=end, step=step, body=body):
                ref = var if var in self._proc_locals else f'self.{var}'
                s = self._expr.int_expr(start)
                e = self._expr.int_expr(end)
                if step is None or (isinstance(step, IntLiteral) and step.value == 1):
                    w.line(f'for {ref} in range({s}, {e} + 1):')
                else:
                    st = self._expr.int_expr(step)
                    w.line(f'for {ref} in range({s}, {e} + 1, {st}):')
                w.indent()
                if body:
                    self.emit_structured(body)
                else:
                    w.line('pass')
                w.dedent()

            case SWhile(condition=cond, body=body):
                w.line(f'while {self._expr.int_expr(cond)}:')
                w.indent()
                if body:
                    self.emit_structured(body)
                else:
                    w.line('pass')
                w.dedent()

            case SWhileTrue(body=body):
                w.line('while True:')
                w.indent()
                if body:
                    self.emit_structured(body)
                else:
                    w.line('pass')
                w.dedent()

            case SIf(condition=cond, then_body=then_b, else_body=else_b):
                w.line(f'if {self._expr.int_expr(cond)}:')
                w.indent()
                if then_b:
                    self.emit_structured(then_b)
                else:
                    w.line('pass')
                w.dedent()
                if else_b:
                    w.line('else:')
                    w.indent()
                    self.emit_structured(else_b)
                    w.dedent()

            case SBreak():
                w.line('break')

            case SContinue():
                w.line('continue')

            case SExit(code=code):
                w.line(f'raise _StaplExit({self._expr.int_expr(code)})')

            case SReturn():
                w.line('return')

            case SDispatch(entry=entry, blocks=blocks, name_to_idx=nti):
                self._emit_dispatch(entry, blocks)

            case _:
                w.line(f'# ??? {type(node).__name__}')

    def _emit_dispatch(self, entry, blocks):
        """Emit block dispatch fallback."""
        w = self._w
        old_in_dispatch = self._in_dispatch
        self._in_dispatch = True
        w.line(f'_block = {entry!r}')
        w.line('while _block is not None:')
        w.indent()
        first = True
        for block in blocks:
            kw = 'if' if first else 'elif'
            w.line(f'{kw} _block == {block.name!r}:')
            w.indent()
            for stmt in block.stmts:
                self._emit_plain_stmt(stmt)
            # Terminal
            match block.terminal:
                case Fallthrough():
                    next_idx = block.index + 1
                    if next_idx < len(blocks):
                        w.line(f'_block = {blocks[next_idx].name!r}')
                    else:
                        w.line('_block = None')
                case Jump(target=t):
                    w.line(f'_block = {t!r}')
                case CondJump(condition=c, target=t):
                    w.line(f'if {self._expr.int_expr(c)}:')
                    w.indent()
                    w.line(f'_block = {t!r}')
                    w.dedent()
                    next_idx = block.index + 1
                    if next_idx < len(blocks):
                        w.line('else:')
                        w.indent()
                        w.line(f'_block = {blocks[next_idx].name!r}')
                        w.dedent()
                    else:
                        w.line('else:')
                        w.indent()
                        w.line('_block = None')
                        w.dedent()
                case BlockExit(code=c):
                    w.line(f'raise _StaplExit({self._expr.int_expr(c)})')
                case BlockReturn():
                    w.line('_block = None')
            w.dedent()
            first = False
        w.dedent()
        self._in_dispatch = old_in_dispatch

    def _emit_plain_stmt(self, stmt):
        """Emit a plain STAPL statement."""
        w = self._w
        e = self._expr

        match stmt:
            case IntegerDecl(name=n, size=sz, init=init):
                ref = n if n in self._proc_locals else f'self.{n}'
                if sz is not None:
                    size_s = e.int_expr(sz)
                    if init is not None:
                        vals = ', '.join(e.int_expr(v) for v in init)
                        w.line(f'{ref} = [{vals}]')
                    else:
                        w.line(f'{ref} = [0] * {size_s}')
                else:
                    if init is not None:
                        w.line(f'{ref} = {e.int_expr(init[0])}')
                    else:
                        w.line(f'{ref} = 0')

            case BooleanDecl(name=n, size=sz, init=init):
                ref = n if n in self._proc_locals else f'self.{n}'
                if sz is not None:
                    size_s = e.int_expr(sz)
                    if init is not None and isinstance(init, BooleanLiteral):
                        vi = self._var_infos.get(n)
                        if vi and vi.init_data is None and vi.scope == 'data':
                            # Externalized to file
                            w.line(f'{ref} = BitArray({init.bit_count}, '
                                   f'(self.DATA_DIR / "{n}.bin").read_bytes())')
                        else:
                            # Inline
                            w.line(f'{ref} = BitArray({init.bit_count}, {init.data!r})')
                    else:
                        w.line(f'{ref} = BitArray({size_s})')
                else:
                    # Scalar boolean
                    if init is not None:
                        w.line(f'{ref} = {e.int_expr(init)}')
                    else:
                        w.line(f'{ref} = 0')

            case AssignStmt(target=t, index=idx, high=h, low=l, value=v):
                ref = t if t in self._proc_locals else f'self.{t}'
                if h is not None and l is not None:
                    # Subrange assignment
                    w.line(f'{ref}.set_subrange({e.int_expr(h)}, {e.int_expr(l)}, '
                           f'{e.bits_expr(v)})')
                elif idx is not None:
                    vi = self._var_infos.get(t)
                    if vi and vi.vtype == 'boolean' and vi.is_array:
                        w.line(f'{ref}.set_bit({e.int_expr(idx)}, {e.int_expr(v)})')
                    else:
                        w.line(f'{ref}[{e.int_expr(idx)}] = {e.int_expr(v)}')
                else:
                    vi = self._var_infos.get(t)
                    if vi and vi.vtype == 'boolean' and vi.is_array:
                        w.line(f'{ref} = {e.bits_expr(v)}')
                    else:
                        w.line(f'{ref} = {e.int_expr(v)}')

            case CallStmt(procedure=p):
                w.line(f'await self._proc_{p.lower()}()')

            case ExitStmt(code=c):
                w.line(f'raise _StaplExit({e.int_expr(c)})')

            case IfStmt(condition=c, then_stmt=t):
                w.line(f'if {e.int_expr(c)}:')
                w.indent()
                self._emit_plain_stmt(t)
                w.dedent()

            case GotoStmt(label=l):
                if self._in_dispatch:
                    w.line(f'_block = {l.upper()!r}')
                    w.line('continue')
                else:
                    w.line(f'pass  # GOTO {l}')

            case PushStmt(value=v):
                w.line(f'self._stack.append({e.int_expr(v)})')

            case PopStmt(target=t, index=idx):
                ref = t if t in self._proc_locals else f'self.{t}'
                if idx is not None:
                    w.line(f'{ref}[{e.int_expr(idx)}] = self._stack.pop()')
                else:
                    w.line(f'{ref} = self._stack.pop()')

            case DrScanStmt(length=l, tdi=tdi, capture=cap, compare=cmp,
                            mask=m, result=r):
                self._emit_scan(False, l, tdi, cap, cmp, m, r)

            case IrScanStmt(length=l, tdi=tdi, capture=cap, compare=cmp,
                            mask=m, result=r):
                self._emit_scan(True, l, tdi, cap, cmp, m, r)

            case DrStopStmt(state=s):
                w.line(f'self._dr_stop = {s!r}')

            case IrStopStmt(state=s):
                w.line(f'self._ir_stop = {s!r}')

            case StateStmt(path=p):
                args = ', '.join(f'{s!r}' for s in p)
                w.line(f'await self._state({args})')

            case WaitStmt(wait_state=ws, cycles=cy, usecs=us,
                          end_state=es):
                parts = []
                parts.append(f'{ws!r}' if ws else 'None')
                parts.append(e.int_expr(cy) if cy else 'None')
                parts.append(e.int_expr(us) if us else 'None')
                parts.append(f'{es!r}' if es else 'None')
                w.line(f'await self._wait({", ".join(parts)})')

            case TrstStmt(cycles=cy, usecs=us):
                cy_s = e.int_expr(cy) if cy else 'None'
                us_s = e.int_expr(us) if us else 'None'
                w.line(f'await self._trst({cy_s}, {us_s})')

            case FrequencyStmt(value=v):
                if v:
                    w.line(f'await self._frequency({e.int_expr(v)})')
                else:
                    w.line(f'await self._frequency(None)')

            case PreDrStmt(count=c, data=d):
                if d:
                    w.line(f'self._pre_dr = ({e.int_expr(c)}, {e.scan_tdi_expr(d)})')
                else:
                    w.line(f'self._pre_dr = ({e.int_expr(c)}, None)')

            case PostDrStmt(count=c, data=d):
                if d:
                    w.line(f'self._post_dr = ({e.int_expr(c)}, {e.scan_tdi_expr(d)})')
                else:
                    w.line(f'self._post_dr = ({e.int_expr(c)}, None)')

            case PreIrStmt(count=c, data=d):
                if d:
                    w.line(f'self._pre_ir = ({e.int_expr(c)}, {e.scan_tdi_expr(d)})')
                else:
                    w.line(f'self._pre_ir = ({e.int_expr(c)}, None)')

            case PostIrStmt(count=c, data=d):
                if d:
                    w.line(f'self._post_ir = ({e.int_expr(c)}, {e.scan_tdi_expr(d)})')
                else:
                    w.line(f'self._post_ir = ({e.int_expr(c)}, None)')

            case PrintStmt(parts=parts):
                args = []
                for p in parts:
                    if isinstance(p, str):
                        args.append(f'{p!r}')
                    else:
                        args.append(e.int_expr(p))
                w.line(f'print({", ".join(args)})')

            case ExportStmt(key=k, value=v):
                w.line(f'await self._export({k!r}, {e.int_expr(v)})')

            case ReturnStmt():
                w.line('return')

            case ForRegion(for_stmt=fs, body=body, next_stmt=ns):
                # ForRegion in dispatch mode: emit as for loop
                ref = fs.var if fs.var in self._proc_locals else f'self.{fs.var}'
                s = e.int_expr(fs.start)
                end_s = e.int_expr(fs.end)
                if fs.step is None or (isinstance(fs.step, IntLiteral) and fs.step.value == 1):
                    w.line(f'for {ref} in range({s}, {end_s} + 1):')
                else:
                    w.line(f'for {ref} in range({s}, {end_s} + 1, {e.int_expr(fs.step)}):')
                w.indent()
                for sub in body:
                    self._emit_plain_stmt(sub)
                if not body:
                    w.line('pass')
                w.dedent()

            case ForStmt(var=v, start=s, end=end, step=st):
                # Bare ForStmt (label inside FOR/NEXT, couldn't extract)
                ref = v if v in self._proc_locals else f'self.{v}'
                w.line(f'{ref} = {e.int_expr(s)}  # FOR {v}')

            case NextStmt(var=v):
                # Bare NextStmt
                ref = v if v in self._proc_locals else f'self.{v}'
                w.line(f'{ref} += 1  # NEXT {v}')

            case _:
                w.line(f'# ??? {type(stmt).__name__}')

    def _emit_scan(self, is_ir, length, tdi, capture, compare, mask, result):
        """Emit IR or DR scan."""
        w = self._w
        e = self._expr
        length_s = e.int_expr(length)

        if is_ir:
            # IRSCAN: emit as _ir_scan with integer value
            w.line(f'await self._ir_scan({e.scan_tdi_as_int(tdi)}, {length_s})')
            if capture:
                w.line(f'# IR capture not supported in transpiled output')
        else:
            # DRSCAN
            has_capture = capture is not None or compare is not None
            if has_capture:
                w.line(f'_tdo = await self._dr_scan('
                       f'{e.scan_tdi_expr(tdi)}, {length_s}, capture=True)')
                if capture is not None:
                    self._emit_capture_store(capture, length_s)
                if compare is not None:
                    cap_ref = self._emit_capture_ref(capture) if capture else '_tdo_bits'
                    if capture is None:
                        w.line(f'_tdo_bits = BitArray({length_s}, _tdo)')
                    cmp_s = e.bits_expr(compare)
                    mask_s = e.bits_expr(mask)
                    result_ref = self._emit_lvalue(result)
                    w.line(f'{result_ref} = 1 if {cap_ref}.compare('
                           f'{cmp_s}, {mask_s}, {length_s}) else 0')
            else:
                w.line(f'await self._dr_scan('
                       f'{e.scan_tdi_expr(tdi)}, {length_s})')

    def _emit_capture_store(self, capture_expr, length_s):
        """Emit code to store captured TDO data."""
        w = self._w
        match capture_expr:
            case ArraySubrange(name=n, high=h, low=l):
                ref = n if n in self._proc_locals else f'self.{n}'
                w.line(f'{ref}.set_subrange({self._expr.int_expr(h)}, '
                       f'{self._expr.int_expr(l)}, '
                       f'BitArray({length_s}, _tdo))')
            case ArrayWhole(name=n):
                ref = n if n in self._proc_locals else f'self.{n}'
                w.line(f'{ref} = BitArray({length_s}, _tdo)')
            case VarRef(name=n):
                ref = n if n in self._proc_locals else f'self.{n}'
                w.line(f'{ref} = BitArray({length_s}, _tdo)')
            case _:
                w.line(f'# capture store: ??? {capture_expr!r}')

    def _emit_capture_ref(self, capture_expr):
        """Emit a reference to the captured data variable."""
        match capture_expr:
            case ArraySubrange(name=n):
                ref = n if n in self._proc_locals else f'self.{n}'
                return ref
            case ArrayWhole(name=n) | VarRef(name=n):
                ref = n if n in self._proc_locals else f'self.{n}'
                return ref
            case _:
                return '_tdo_bits'

    def _emit_lvalue(self, expr):
        """Emit an lvalue expression (assignment target)."""
        match expr:
            case VarRef(name=n):
                return n if n in self._proc_locals else f'self.{n}'
            case ArrayIndex(name=n, index=idx):
                ref = n if n in self._proc_locals else f'self.{n}'
                return f'{ref}[{self._expr.int_expr(idx)}]'
            case _:
                return f'# ??? lvalue {expr!r}'


# ============================================================
# Top-level file emission
# ============================================================

def _collect_proc_locals(proc):
    """Collect local variable names declared in a procedure."""
    locals_set = set()
    for stmt in proc.statements:
        if isinstance(stmt, IntegerDecl):
            locals_set.add(stmt.name)
        elif isinstance(stmt, BooleanDecl):
            locals_set.add(stmt.name)
    return locals_set


def _collect_data_vars(proc, program):
    """Collect variable names from DATA blocks that a procedure USES."""
    data_vars = set()
    for data_name in proc.uses:
        data_block = program.data_blocks.get(data_name)
        if data_block:
            for stmt in data_block.statements:
                if isinstance(stmt, (IntegerDecl, BooleanDecl)):
                    data_vars.add(stmt.name)
    return data_vars


def transpile(program: Program, config: TranspileConfig | None = None,
              source_name: str = "unknown") -> tuple[str, dict[str, bytes]]:
    """Transpile a STAPL program to Python source + data files.

    Args:
        program: Parsed STAPL Program.
        config: Transpilation options.
        source_name: Original filename for documentation.

    Returns:
        (python_source, data_files) where data_files maps
        "filename.bin" → bytes for external data.
    """
    if config is None:
        config = TranspileConfig()

    var_infos, data_files = _analyze_variables(program, config)

    w = _Writer()

    # --- File header ---
    w.line(f'"""Transpiled from: {source_name}"""')
    w.line()
    w.line('import asyncio')
    w.line('from pathlib import Path')
    w.line()
    w.line('import asyncclick as click')
    w.line()
    w.line('from acrobe.adapter.model import HwRoot, UsbEnumerator')
    w.line('from acrobe.bitstring import BitString')
    w.line('from acrobe.component import Component')
    w.line('from acrobe.stapl.interpreter import BitArray')
    w.line()
    w.line()
    w.line('class _StaplExit(Exception):')
    w.line('    """Raised by EXIT statements to terminate execution."""')
    w.line('    def __init__(self, code):')
    w.line('        self.code = code')
    w.line('        super().__init__(f"EXIT {code}")')
    w.line()
    w.line()

    # --- Notes ---
    _emit_notes(w, program)

    # --- Class definition ---
    w.line('class TranspiledProgram:')
    w.indent()
    w.line(f'"""Transpiled from: {source_name}')
    w.line()
    if program.actions:
        w.line('Actions:')
        for name, action in program.actions.items():
            desc = f'  {action.description}' if action.description else ''
            w.line(f'    {name}{desc}')
    w.line('"""')
    w.line()
    w.line('DATA_DIR = Path(__file__).parent / "data"')
    w.line()

    # --- Constructor ---
    _emit_constructor(w)

    # --- JTAG helpers ---
    _emit_helpers(w)

    # --- Data initialization methods ---
    _emit_data_init(w, program, var_infos, config)

    # --- Procedures ---
    for proc_name, proc in program.procedures.items():
        _emit_procedure(w, proc, program, var_infos, config)

    # --- Actions ---
    for action_name, action in program.actions.items():
        _emit_action(w, action)

    w.dedent()  # end class
    w.line()
    w.line()

    # --- CLI ---
    _emit_cli(w, program)

    return w.text(), data_files


def _emit_notes(w, program):
    """Emit NOTE metadata as module-level comments."""
    if program.notes:
        for note in program.notes:
            w.line(f'# NOTE {note.key}: {note.value}')
        w.line()
        w.line()


def _emit_constructor(w):
    w.line('def __init__(self, tap):')
    w.indent()
    w.line('self.tap = tap')
    w.line('self._ir_value = 0')
    w.line('self._ir_length = 0')
    w.line('self._stack = []')
    w.line('self._dr_stop = "IDLE"')
    w.line('self._ir_stop = "IDLE"')
    w.line('self._pre_dr = (0, None)')
    w.line('self._post_dr = (0, None)')
    w.line('self._pre_ir = (0, None)')
    w.line('self._post_ir = (0, None)')
    w.line('self._initialized = set()')
    w.line('self._did_operation = False')
    w.dedent()
    w.line()


def _emit_helpers(w):
    w.line('async def _ir_scan(self, value, length):')
    w.indent()
    w.line('"""Shift IR register via JTAG interface directly."""')
    w.line('self._did_operation = True')
    w.line('self._ir_value = value')
    w.line('self._ir_length = length')
    w.line('tdi = BitString(value.to_bytes((length + 7) // 8, "little"), length)')
    w.line('from acrobe.protocol.jtag import CaptureIr, Shift, Run')
    w.line('iface = self.tap._interface')
    w.line('await iface.post(CaptureIr())')
    w.line('await iface.post(Shift(tdi, read_tdo=False))')
    w.line('await iface.post(Run(1))')
    w.line('self.tap._current_ir = None')
    w.dedent()
    w.line()

    w.line('async def _dr_scan(self, tdi, length, capture=False):')
    w.indent()
    w.line('"""Shift DR via JTAG interface directly. Returns bytes if capture."""')
    w.line('self._did_operation = True')
    w.line('tdi_bs = BitString(bytes(tdi), length)')
    w.line('from acrobe.protocol.jtag import CaptureDr, Shift, Run')
    w.line('iface = self.tap._interface')
    w.line('await iface.post(CaptureDr())')
    w.line('shift_op = Shift(tdi_bs, read_tdo=capture)')
    w.line('result = await iface.post(shift_op)')
    w.line('await iface.post(Run(1))')
    w.line('if capture:')
    w.indent()
    w.line('return bytes(result.tdo.data[:((length + 7) // 8)])')
    w.dedent()
    w.line('return None')
    w.dedent()
    w.line()

    w.line('async def _state(self, *states):')
    w.indent()
    w.line('"""Transition TAP through states."""')
    w.line('from acrobe.protocol.jtag import Run')
    w.line('for state in states:')
    w.indent()
    w.line("if state == 'RESET':")
    w.indent()
    w.line('await self._trst(None, None)')
    w.dedent()
    w.line('else:')
    w.indent()
    w.line('await self.tap._interface.post(Run(1))')
    w.dedent()
    w.dedent()
    w.dedent()
    w.line()

    w.line('async def _wait(self, wait_state, cycles, usecs, end_state):')
    w.indent()
    w.line('"""Wait with optional state transitions and delay."""')
    w.line('from acrobe.protocol.jtag import Run')
    w.line('if wait_state:')
    w.indent()
    w.line('await self._state(wait_state)')
    w.dedent()
    w.line('if cycles:')
    w.indent()
    w.line('await self.tap._interface.post(Run(cycles))')
    w.dedent()
    w.line('if usecs:')
    w.indent()
    w.line('await asyncio.sleep(usecs / 1_000_000)')
    w.dedent()
    w.line('if end_state and end_state != wait_state:')
    w.indent()
    w.line('await self._state(end_state)')
    w.dedent()
    w.dedent()
    w.line()

    w.line('async def _trst(self, cycles, usecs):')
    w.indent()
    w.line('"""TAP reset via TMS sequence.')
    w.line()
    w.line('Warning: TRST after other operations may invalidate chain')
    w.line('state, requiring full re-enumeration.')
    w.line('"""')
    w.line('if self._did_operation:')
    w.indent()
    w.line("import logging")
    w.line("logging.getLogger(__name__).warning(")
    w.line("    'TRST after operations: chain state may need re-enumeration')")
    w.dedent()
    w.line('self._did_operation = True')
    w.line('# 5 TMS=1 clocks guarantees reset from any state')
    w.line('from acrobe.protocol.jtag import Run')
    w.line('await self.tap._interface.post(Run(5))')
    w.dedent()
    w.line()

    w.line('async def _frequency(self, hertz):')
    w.indent()
    w.line('"""Set TCK frequency."""')
    w.line('if hertz is not None and self.tap.max_freq is not None:')
    w.indent()
    w.line('self.tap.max_freq = min(hertz, self.tap.max_freq)')
    w.dedent()
    w.line('elif hertz is not None:')
    w.indent()
    w.line('self.tap.max_freq = hertz')
    w.dedent()
    w.dedent()
    w.line()

    w.line('async def _export(self, key, value):')
    w.indent()
    w.line('"""Export a key/value pair."""')
    w.line('print(f"EXPORT {key} = {value}")')
    w.dedent()
    w.line()


def _emit_data_init(w, program, var_infos, config):
    """Emit _init_data_XXX methods for each DATA block."""
    for block_name, data_block in program.data_blocks.items():
        w.line(f'def _init_data_{block_name.lower()}(self):')
        w.indent()
        w.line(f'"""Initialize DATA block {block_name}."""')
        w.line(f'if {block_name!r} in self._initialized:')
        w.indent()
        w.line('return')
        w.dedent()
        w.line(f'self._initialized.add({block_name!r})')

        expr_em = _ExprEmitter(var_infos, set())
        for stmt in data_block.statements:
            match stmt:
                case IntegerDecl(name=n, size=sz, init=init):
                    if sz is not None:
                        size_s = expr_em.int_expr(sz)
                        if init is not None:
                            vals = ', '.join(expr_em.int_expr(v) for v in init)
                            w.line(f'self.{n} = [{vals}]')
                        else:
                            w.line(f'self.{n} = [0] * {size_s}')
                    else:
                        if init is not None:
                            w.line(f'self.{n} = {expr_em.int_expr(init[0])}')
                        else:
                            w.line(f'self.{n} = 0')

                case BooleanDecl(name=n, size=sz, init=init):
                    vi = var_infos.get(n)
                    if sz is not None:
                        if (init is not None and isinstance(init, BooleanLiteral)
                                and vi and vi.init_data is None):
                            # Externalized
                            w.line(f'self.{n} = BitArray({init.bit_count}, '
                                   f'(self.DATA_DIR / "{n}.bin").read_bytes())')
                        elif init is not None and isinstance(init, BooleanLiteral):
                            # Inline (small)
                            w.line(f'self.{n} = BitArray({init.bit_count}, '
                                   f'{init.data!r})')
                        else:
                            size_s = expr_em.int_expr(sz)
                            w.line(f'self.{n} = BitArray({size_s})')
                    else:
                        if init is not None:
                            w.line(f'self.{n} = {expr_em.int_expr(init)}')
                        else:
                            w.line(f'self.{n} = 0')

        w.dedent()
        w.line()


def _emit_procedure(w, proc, program, var_infos, config):
    """Emit a procedure as an async method."""
    proc_locals = _collect_proc_locals(proc)
    data_vars = _collect_data_vars(proc, program)

    w.line(f'async def _proc_{proc.name.lower()}(self):')
    w.indent()
    w.line(f'"""PROCEDURE {proc.name}"""')

    # Initialize DATA blocks
    for data_name in proc.uses:
        if data_name in program.data_blocks:
            w.line(f'self._init_data_{data_name.lower()}()')

    # Build control flow
    stmts = proc.statements
    labels = proc.labels

    # Preprocess FOR/NEXT
    try:
        processed, adjusted_labels = _preprocess_for_next(stmts, labels)
    except AssertionError:
        # Labels inside FOR/NEXT: fall back to flat emission
        processed = stmts
        adjusted_labels = labels

    # Build basic blocks
    blocks, name_to_idx = _build_blocks(processed, adjusted_labels)

    # Recover control flow
    structured = _recover_control_flow(blocks, name_to_idx)

    # Emit
    expr_em = _ExprEmitter(var_infos, proc_locals)
    stmt_em = _StmtEmitter(w, expr_em, var_infos, proc_locals, program)
    stmt_em.emit_structured(structured)

    # Ensure method body is not empty
    if not structured:
        w.line('pass')

    w.dedent()
    w.line()


def _emit_action(w, action):
    """Emit an action as an async method."""
    desc = f': {action.description}' if action.description else ''
    w.line(f'async def action_{action.name.lower()}(self):')
    w.indent()
    w.line(f'"""ACTION {action.name}{desc}"""')
    w.line('try:')
    w.indent()
    for proc_name, modifier in action.procedures:
        if modifier == 'OPTIONAL':
            w.line(f'# OPTIONAL: await self._proc_{proc_name.lower()}()')
        elif modifier == 'RECOMMENDED':
            w.line(f'await self._proc_{proc_name.lower()}()  # RECOMMENDED')
        else:
            w.line(f'await self._proc_{proc_name.lower()}()')
    w.line('return 0')
    w.dedent()
    w.line('except _StaplExit as e:')
    w.indent()
    w.line('return e.code')
    w.dedent()
    w.dedent()
    w.line()


def _emit_cli(w, program):
    """Emit asyncclick CLI entry point."""
    # Collect action names for help text
    action_names = list(program.actions.keys())
    action_help = ', '.join(action_names) if action_names else 'none'

    w.line('@click.command()')
    w.line("@click.option('-r', '--root', 'root_path', required=True,")
    w.line("              help='Path to TAP (e.g. tei-/jtag/0/0)')")
    w.line("@click.option('-a', '--action', 'action_name', default=None,")
    w.line(f"              help='Action to execute (available: {action_help})')")
    w.line('async def main(root_path, action_name):')
    w.indent()
    w.line('"""Execute transpiled STAPL program."""')
    w.line('import logging')
    w.line('from acrobe import log')
    w.line('from acrobe.protocol.jtag import Tap')
    w.line()
    w.line('log.setup(level=logging.INFO)')
    w.line()
    w.line('hw_root = HwRoot()')
    w.line('hw_root.add_enumerator(UsbEnumerator())')
    w.line()
    w.line("parts = root_path.strip('/').split('/')")
    w.line('leaf = await hw_root.child_summon(*parts)')
    w.line('if isinstance(leaf, Component):')
    w.indent()
    w.line('await leaf.start_tree()')
    w.dedent()
    w.line()
    w.line('if not isinstance(leaf, Tap):')
    w.indent()
    w.line("click.echo(f'Root resolved to {type(leaf).__name__}, expected a Tap.', err=True)")
    w.line("click.echo('Use a path that resolves to a TAP, e.g. adapter/jtag/0/0', err=True)")
    w.line('raise SystemExit(1)')
    w.dedent()
    w.line()
    w.line('tap = leaf')
    w.line('prog = TranspiledProgram(tap)')
    w.line()
    w.line('if action_name is None:')
    w.indent()
    if action_names:
        w.line(f'action_name = {action_names[0]!r}')
    else:
        w.line("click.echo('No actions available.', err=True)")
        w.line('raise SystemExit(1)')
    w.dedent()
    w.line()
    w.line("action_method = getattr(prog, f'action_{action_name.lower()}', None)")
    w.line('if action_method is None:')
    w.indent()
    w.line("click.echo(f'Unknown action: {action_name}', err=True)")
    w.line('raise SystemExit(1)')
    w.dedent()
    w.line()
    w.line('exit_code = await action_method()')
    w.line('if exit_code:')
    w.indent()
    w.line("click.echo(f'Action {action_name} failed with exit code {exit_code}', err=True)")
    w.line('raise SystemExit(exit_code)')
    w.dedent()
    w.line("click.echo(f'Action {action_name} completed successfully.')")
    w.dedent()
    w.line()
    w.line()
    w.line("if __name__ == '__main__':")
    w.indent()
    w.line('main()')
    w.dedent()
