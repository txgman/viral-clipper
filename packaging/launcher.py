"""Entry point for the packaged ViralClipper.exe."""
import sys
import traceback

if __name__ == "__main__":
    try:
        from app.__main__ import main
        main()
    except KeyboardInterrupt:
        pass
    except Exception:  # keep the window open so the user can read what went wrong
        traceback.print_exc()
        if getattr(sys, "frozen", False):
            input("\nViral Clipper hit an error (details above). Press Enter to close…")
        sys.exit(1)
