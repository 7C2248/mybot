"""CLI entry point; conversations and memory policy are owned by the local API."""
from cli.client import main, parse_args, chat


if __name__ == '__main__':
    raise SystemExit(main())
