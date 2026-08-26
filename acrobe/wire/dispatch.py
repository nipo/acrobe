"""Server-side request dispatch.

`handle_request` posts each op in a Request to a Batcher node,
gathers the futures, and packages the outcomes into a Response.

Errors:

* If the op's future raises an instance of a registered `@wire.error`
  class, it goes into `Response.errors` as-is.
* Anything else gets wrapped in `InternalError(representation=repr(exc))`
  before transit. This protects the client from server-side leakage
  (private exception classes, traceback strings) while keeping a
  meaningful summary.
"""

from .errors import InternalError
from .frame import Request, Response
from .session import Session


async def handle_request(node, session: Session, request: Request) -> Response:
    """Dispatch `request` against `node`, returning the Response frame.

    Each op is `node.post`-ed; futures are awaited in batch order.
    `node` MUST be a Batcher.
    """
    futures = [node.post(op) for op in request.batch]

    results: dict[int, object] = {}
    errors: dict[int, object] = {}

    for idx, fut in enumerate(futures):
        try:
            value = await fut
        except Exception as exc:
            entry = session.registry.try_lookup_by_class(type(exc))
            if entry is not None and entry.kind == "error":
                errors[idx] = exc
            else:
                errors[idx] = InternalError(representation=repr(exc))
        else:
            results[idx] = value

    return Response(req_id=request.req_id, results=results, errors=errors)
