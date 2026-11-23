from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = PROJECT_ROOT / "shared"


def test_default_output_root_is_anchored_to_project_from_any_cwd(tmp_path):
    env = os.environ.copy()
    env.pop("BOSS_HR_OUTPUT_DIR", None)
    env["HR_AGENT_DISABLE_DOTENV"] = "1"

    code = (
        "import sys; "
        f"sys.path.insert(0, {str(SHARED_DIR)!r}); "
        "from output_manager import OUTPUT_ROOT; "
        "print(OUTPUT_ROOT)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert Path(result.stdout.strip()) == PROJECT_ROOT / "data" / "runs"
