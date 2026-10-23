"""Atomic output publication that tolerates transient Windows reader locks."""
import tempfile
import time
from contextlib import contextmanager, suppress
from pathlib import Path


class OutputLockedError(PermissionError):
    """The old output is intact and the prepared replacement is retained."""


def replace_with_retry(source, destination, attempts=12, *, preserve_hint=True):
    source, destination = Path(source), Path(destination)
    delay = 0.1
    for attempt in range(attempts):
        try:
            source.replace(destination)
            return
        except PermissionError as exc:
            if attempt == attempts - 1:
                detail = (f'The completed replacement is preserved at {source}. '
                          'Close the file or pause the reader, then retry publication; recalculation is not required.'
                          if preserve_hint else 'Close the file or pause the reader, then retry the refresh.')
                raise OutputLockedError(
                    f'Cannot replace {destination}: another program may have it open. ' + detail
                ) from exc
            time.sleep(delay)
            delay = min(delay * 2, 2)


@contextmanager
def prepared_output(destination, *, newline=None):
    """Yield a unique, same-directory output stream; retain completed results."""
    destination = Path(destination)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline=newline,
                                     dir=destination.parent, prefix=destination.name + '.',
                                     suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            yield stream, temporary
        except BaseException:
            stream.close()
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise
