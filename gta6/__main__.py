import sys

if "--selftest" in sys.argv:
    from .selftest import run
    run()
else:
    from .app import main
    main()
