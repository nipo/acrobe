import asyncio
import os
import sys

import asyncclick as click

from . import base


@base.cli.command(
    context_settings=dict(ignore_unknown_options=True),
    help="Run a script or module with acrobe context (logging, plugins, asyncio)",
)
@click.option("--module", "-m", is_flag=True,
              help="Run a module rather than a script")
@click.argument("entry", type=str)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
async def run(module, entry, args):
    if module:
        ns = _runmodule(entry, args)
    else:
        if not os.path.exists(entry):
            click.echo(f"Error: {entry} does not exist", err=True)
            sys.exit(1)
        sys.path[0] = os.path.dirname(os.path.abspath(entry))
        ns = _runscript(entry, args)

    main = ns.get("main")
    if main is not None and asyncio.iscoroutinefunction(main):
        await main()


def _runmodule(module_name, args):
    import runpy

    mod_name, mod_spec, code = runpy._get_module_details(module_name)
    mainpyfile = _path_normalize(code.co_filename)
    sys.argv[:] = [mainpyfile, *args]

    import __main__
    __main__.__dict__.clear()
    __main__.__dict__.update({
        "__name__": "__main__",
        "__file__": mainpyfile,
        "__package__": mod_spec.parent,
        "__loader__": mod_spec.loader,
        "__spec__": mod_spec,
        "__builtins__": __builtins__,
    })

    exec(code, __main__.__dict__, __main__.__dict__)
    return __main__.__dict__


def _runscript(filename, args):
    mainpyfile = _path_normalize(filename)
    sys.argv[:] = [mainpyfile, *args]

    import __main__
    __main__.__dict__.clear()
    __main__.__dict__.update({
        "__name__": "__main__",
        "__file__": mainpyfile,
        "__builtins__": __builtins__,
    })

    with open(filename, "rb") as fp:
        code = compile(fp.read(), filename, "exec")

    exec(code, __main__.__dict__, __main__.__dict__)
    return __main__.__dict__


def _path_normalize(filename):
    if filename.startswith("<") and filename.endswith(">"):
        return filename
    return os.path.normcase(os.path.abspath(filename))
