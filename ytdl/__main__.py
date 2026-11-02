"""Allow ``python -m ytdl`` to behave exactly like the ``ytdl`` console script."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    main()
