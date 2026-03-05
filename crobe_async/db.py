import inspect
import operator


class NoMatch(Exception):
    def __init__(self, db_name, criterion):
        self.db_name = db_name
        self.criterion = criterion

    def __str__(self):
        return f"No match for {self.criterion!r} in {self.db_name}"


class Db:
    def __init__(self, name, eq_func=operator.eq):
        self.name = name
        self._registry = {}
        self._eq_func = eq_func
        self._default = None

    def register(self, *ids):
        """Decorator: register a handler under one or more IDs."""
        def decorator(obj):
            for id in ids:
                self._registry.setdefault(id, []).append(obj)
            return obj
        return decorator

    def register_default(self, obj):
        """Decorator: register a default/fallback handler."""
        self._default = obj
        return obj

    def get(self, id, allow_default=True):
        """Return list of handlers matching id. Raises NoMatch if none found."""
        for key in self._registry:
            if self._eq_func(key, id):
                return self._registry[key]
        if self._default is not None and allow_default:
            return [self._default]
        raise NoMatch(self.name, id)

    def call(self, id, *args, allow_default=True, **kwargs):
        """Try each matching handler in order. First one that succeeds wins.
        Handlers may raise NoMatch to defer to the next."""
        handlers = self.get(id, allow_default=allow_default)
        errors = []
        for handler in handlers:
            try:
                return handler(*args, **kwargs)
            except NoMatch as e:
                errors.append(e)
        if len(errors) == 1:
            raise NoMatch(self.name, id) from errors[0]
        raise NoMatch(self.name, id)

    async def acall(self, id, *args, allow_default=True, **kwargs):
        """Async version of call. Awaits awaitables returned by handlers."""
        handlers = self.get(id, allow_default=allow_default)
        errors = []
        for handler in handlers:
            try:
                result = handler(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                return result
            except NoMatch as e:
                errors.append(e)
        if len(errors) == 1:
            raise NoMatch(self.name, id) from errors[0]
        raise NoMatch(self.name, id)
