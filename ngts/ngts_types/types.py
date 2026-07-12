from typing import Callable

CleanUpT = Callable[[Callable[[], None]], None]
LinkDiagnosticT = dict[str, dict[str, str]]
