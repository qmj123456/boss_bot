"""Retry failed AI analyses in one run without opening BOSS or sending messages."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.resume_analyzer import ResumeAnalyzer
from boss_hr.config_manager import get_config
from shared.analysis_recovery import retry_saved_analyses


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screening-results", required=True, type=Path,
                        help="已结束 run 的 process/screening_results.json 绝对路径")
    args = parser.parse_args()
    screening = json.loads(args.screening_results.read_text(encoding="utf-8"))
    job = screening.get("job")
    if not job:
        parser.error("筛选文件缺少 job，无法确定岗位要求")
    config = get_config()
    profile = config.load_job_profile(job)
    if not profile:
        parser.error("没有找到该岗位配置，禁止使用其他岗位要求回补")
    requirement = dict(config.section("recruitment"))
    requirement.update(profile)
    requirement["job_name"] = job
    stats = retry_saved_analyses(
        args.screening_results, analyzer=ResumeAnalyzer(config=config),
        requirement=requirement, job_name=job,
        attempts=config.get("performance.ai.total_attempts", 3),
        retry_delay=config.get("performance.ai.retry_delay_seconds", 2.0),
        retry_max_delay=config.get("performance.ai.retry_max_delay_seconds", 8.0),
    )
    print(json.dumps(stats, ensure_ascii=False))
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
