"""Test fixture shaped like the WB shell supervisor: argv ``python … shell_g2/lifecycle.py --supervisor …``.

Not the product supervisor: it only runs ``../subreaper.py`` so the process argv has the
supervisor shape that ``managed_ancestor`` must recognise (HB-03, seam S9).
"""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).resolve().parents[1] / "subreaper.py"), run_name="__main__")
