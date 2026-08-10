from collections import defaultdict
from typing import Callable, TypeVar, cast

K = TypeVar("K")
V = TypeVar("V")


class DefaultDict(defaultdict[K, V]):
    """
    collections.defaultdict is a class that inherits the builtin dict.

    with builtin dict - when trying to access a key which doesn't exist, exception is raised.
    with collections.defaultdict - it adds the desired key with a default value, which the user should specify
        when initializing the defaultdict object.

    however, with collections.defaultdict, the default value can only be a const and can't be based on the key.

    with DefaultDict, can set the default value to depend on the key.

    Example:
        dd = DefaultDict(lambda k: "hello to " + k).
        --> assuming the keys are strings, every access to a new key will generate "hello to <new key>"
    """

    def __init__(self, default_factory: Callable[[K], V]) -> None:
        # The stdlib stubs type default_factory as a zero-arg callable, but the
        # only caller of the factory is our __missing__ below, which passes the
        # key - so storing a one-arg callable is safe. The cast avoids ty's
        # no-matching-overload at every DefaultDict(...) construction site.
        super().__init__(cast("Callable[[], V]", default_factory))

    def __missing__(self, key: K) -> V:
        factory = cast("Callable[[K], V]", self.default_factory)
        self[key] = value = factory(key)
        return value
