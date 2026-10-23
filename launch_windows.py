"""Windows launcher, also supports MONITOR_PYTHON for a custom Python install."""
import sys

if sys.version_info < (3, 10):
    raise SystemExit('Python 3.10 or newer is required.')

if __name__ == '__main__':
    from server import main
    main()
