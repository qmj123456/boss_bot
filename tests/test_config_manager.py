from pathlib import Path

from boss_hr.config_manager import ConfigManager
from boss_hr.commands._argparse_helpers import resolve_encrypt_job_id


def test_load_default_config_from_project_config():
    cfg = ConfigManager()

    assert cfg.get("app.log_level") == "INFO"
    assert cfg.get("performance.screenshot.total_attempts") == 2
    assert cfg.get("performance.ai.total_attempts") == 3
    assert cfg.get("performance.screenshot.scroll_ratio") == [0.68, 0.78]
    assert cfg.get("ai.vision_timeout_seconds") == 60
    assert cfg.get("ai.vision_max_tokens") == 8000
    assert cfg.get("recruitment.daily_send_limit") == 0  # 测试阶段不限制，上线后改回 20
    assert cfg.load_prompt("review_resume.md").startswith("# 简历硬性条件筛选 Prompt")


def test_relative_path_resolves_against_runtime_dir(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text(
        "app:\n  database:\n    path: data/hr_agent.db\n",
        encoding="utf-8",
    )

    cfg = ConfigManager(config_dir=config_dir, runtime_dir=tmp_path)

    assert cfg.get_path("app.database.path") == tmp_path / "data" / "hr_agent.db"


def test_env_placeholder_expansion(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ai.yaml").write_text(
        "ai:\n  api_key: ${ENV:TEST_AI_KEY}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_AI_KEY", "secret-from-env")

    cfg = ConfigManager(config_dir=config_dir, runtime_dir=tmp_path)

    assert cfg.get("ai.api_key") == "secret-from-env"


def test_default_job_profile_name_uses_app_setting(tmp_path):
    config_dir = tmp_path / "config"
    jobs_dir = config_dir / "jobs"
    jobs_dir.mkdir(parents=True)
    (config_dir / "app.yaml").write_text(
        "app:\n  default_job_name: 全职主播\n",
        encoding="utf-8",
    )
    (jobs_dir / "全职主播.yaml").write_text(
        "recruitment:\n  job_name: 主播\n  boss_display_name: 高级主播\n",
        encoding="utf-8",
    )

    cfg = ConfigManager(config_dir=config_dir, runtime_dir=tmp_path)

    assert cfg.default_job_profile_name() == "全职主播"
    assert cfg.get("recruitment.boss_display_name") == "高级主播"


def test_dotenv_is_loaded_from_runtime_dir(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ai.yaml").write_text(
        "ai:\n  api_key: ${OPENAI_API_KEY}\n  base_url: ${OPENAI_BASE_URL}\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=secret-from-dotenv\nOPENAI_BASE_URL=\"https://example.test/v1\"\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    cfg = ConfigManager(config_dir=config_dir, runtime_dir=tmp_path)

    assert cfg.get("ai.api_key") == "secret-from-dotenv"
    assert cfg.get("ai.base_url") == "https://example.test/v1"


def test_project_dotenv_overrides_inherited_env(tmp_path, monkeypatch):
    """项目 .env 优先于父进程继承的系统环境变量，避免旧 key 偷偷覆盖。"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ai.yaml").write_text(
        "ai:\n  api_key: ${OPENAI_API_KEY}\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("OPENAI_API_KEY=secret-from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-from-system-env")

    cfg = ConfigManager(config_dir=config_dir, runtime_dir=tmp_path)

    assert cfg.get("ai.api_key") == "secret-from-dotenv"


def test_cli_entry_loads_dotenv_before_encrypt_job_id_validation(monkeypatch):
    import boss_hr.cli as cli

    monkeypatch.delenv("BOSS_HR_ENCRYPT_JOB_ID", raising=False)
    monkeypatch.setenv("BOSS_HR_ENCRYPT_JOB_ID", "eid-from-dotenv-loader")

    assert resolve_encrypt_job_id(None) == "eid-from-dotenv-loader"
    assert "automate" in cli.COMMANDS


def test_tiktok_job_uses_fixed_greet_message():
    message = ConfigManager().load_job_profile("tiktok运营")["greet_message"]

    assert message == (
        "哈喽，我们在招tiktok运营！\n"
        "公司重点项目为tiktok！资金充足！年gmv过亿！\n"
        "精品运营！\n"
        "无责底薪+提成+年终奖\n"
        "岗位优势：\n"
        "1、有成熟的合伙人晋升机制；\n"
        "2、成熟的产品开发团队，能充分支持选品决策，稳定的供应链，重视产品质量。\n"
        "3、强大的内容团队\n"
        "4、高竞争力的薪酬制度，店铺月度销售额100W，岗位年收入在30~50W\n"
        "5、这里赋予你最大的运营权限，充分信任你的能力，让你自由掌控策略，发挥无限潜力！\n"
        "6、团队氛围活跃简单，互相分享学习是常态！\n"
        "7、全开放AI资源，不限token！\n"
        "方便发份简历详细聊下吗\n"
    )


def test_amazon_job_uses_fixed_greet_message():
    message = ConfigManager().load_job_profile("亚马逊运营")["greet_message"]

    assert message == (
        "哈喽，我们在招亚马逊运营！\n"
        "公司重点项目为亚马逊！资金充足！年gmv过亿！\n"
        "品类不限，给你充分的自主空间！\n"
        "纯白帽！精品运营！客单价30-90美金！管理10条左右链接！\n"
        "无责底薪+提成（回款1-5%）+年终奖\n"
        "岗位优势：\n"
        "1、有成熟的合伙人晋升机制；\n"
        "2、成熟的产品开发团队，能充分支持选品决策，稳定的供应链，重视产品质量。\n"
        "3、强大的视觉团队，团队成员都是从视觉大厂出来的；\n"
        "4、高竞争力的薪酬制度，店铺月度销售额100W，岗位年收入在30~50W\n"
        "5、这里赋予你最大的运营权限，充分信任你的能力，让你自由掌控策略，发挥无限潜力！\n"
        "6、团队氛围活跃简单，互相分享学习是常态！\n"
        "7、全开放AI资源，不限token！\n"
        "方便发份简历详细聊下吗\n"
    )


def test_customer_service_profile_has_isolated_filters_and_fixed_greet_message():
    profile = ConfigManager().load_job_profile("客服")

    assert profile["boss_display_name"] == ""
    assert profile["card_prescreen"] == {
        "require_work_experience": True,
        "enabled": True,
        "active_status_keywords": ["刚刚活跃", "在线"],
        "max_age": 27,
        "min_degree": "大专",
        "city_keyword": "深圳",
        "role_keywords": ["客服"],
        "latest_work_end_not_before": "2026.07",
        "recent_work_limit": 2,
        "recent_work_position_keywords": ["客服"],
        "recent_work_min_months": 8,
    }
    assert profile["boss_filter"] == {
        "not_viewed_14d": True,
        "active_status": ["刚刚活跃", "在线"],
        "employment_status": ["离职-随时到岗"],
        "age_max": 27,
        "degree": "大专",
    }
    assert profile["resume_analysis"] == {
        "employment_status_whitelist": [],
        "expected_salary_min": 5000,
        "expected_salary_max": 6000,
        "expected_salary_basis": "lower_bound",
        "expected_salary_accept_negotiable": True,
    }
    assert profile["boss_filter"]["employment_status"] == ["离职-随时到岗"]
    assert "团队排名、转化、售后" in profile["additional_requirements"]
    assert profile["greet_message"] == (
        "您好，公司产品为高价位手机壳，岗位售前售后都会涉及（售后工作不扯皮直接退换货），日均接待量200左右，\n"
        "\n"
        "公司位置在民治，展滔科技大厦。\n"
        "\n"
        "试用期3个月\n"
        "\n"
        "无责底薪5000+提成，综合月收入6000+\n"
        "\n"
        "工作时间08:00-17:00/12:00-21:00/16:00-23:59，月休4-5天，三班倒，按需排班，打车报销\n"
        "\n"
        "入职缴纳社保\n"
        "\n"
        "这边感兴趣的话方便发一份简历详细了解一下嘛。\n"
    )
    assert "在职周期最长的一段" in profile["additional_requirements"]
    assert "至少有2种不同指标带明确数字成果" in profile["additional_requirements"]


def test_anchor_profiles_require_female_boss_filter():
    cfg = ConfigManager()

    fulltime = cfg.load_job_profile("全职主播")
    parttime = cfg.load_job_profile("兼职主播")

    assert fulltime["boss_filter"]["gender"] == "女"
    assert parttime["boss_filter"]["gender"] == "女"
    assert fulltime["card_prescreen"]["salary_low_max"] == 13000
    assert "salary_max" not in fulltime["card_prescreen"]
    assert "salary_low_max" not in parttime["card_prescreen"]
    assert "主播、带货主播、抖音主播或电商主播" in fulltime["experience_required"]
    assert "仅写销冠、第一、前三、晋升、培训新主播、带新人" not in fulltime["additional_requirements"]
    assert "recent_outcomes" not in fulltime["resume_analysis"]
    assert "主播、带货主播、抖音主播或电商主播" in parttime["experience_required"]
    assert "最近两份工作经历必须各自至少有1项明确数字业务成果" not in parttime["additional_requirements"]
    assert "recent_outcomes" not in parttime["resume_analysis"]
    assert parttime["card_prescreen"]["city_keyword"] == "深圳"
    assert parttime["card_prescreen"]["role_keyword"] == "主播"
    assert parttime["card_prescreen"]["require_city_and_role_in_same_field"] is True


def test_junior_video_profile_drops_cet6_degree_and_employment_status():
    profile = ConfigManager().load_job_profile("初级剪拍")

    assert "degree_required" not in profile
    assert all("CET-6" not in item for item in profile["skill_requirements"])
    assert "CET-6" not in profile["additional_requirements"]
    assert profile["card_prescreen"]["max_age"] is None
    assert profile["resume_analysis"]["employment_status_blacklist"] == []
    outcomes = profile["resume_analysis"]["recent_outcomes"]
    assert outcomes["min_metrics"] == 1
    assert outcomes["distinct_metrics"] is False
    assert profile["experience_required"].startswith("27届应届生")
    assert "至少有一段工作经历具备至少1项数据成果" in profile["additional_requirements"]
    assert "金额、消耗算有效数据成果" in profile["additional_requirements"]
    assert "实习经历里具有剪辑关键词" not in profile["additional_requirements"]
    assert "运营个人自媒体账号且有成果" not in profile["additional_requirements"]
    assert "有效成果包括带具体数字的转化率" not in profile["additional_requirements"]
    assert "3条视频" not in profile["additional_requirements"]


def test_operation_assistant_filters_public_undergraduate_school_without_degree_option():
    profile = ConfigManager().load_job_profile("运营助理")

    assert profile["degree_required"] == "学历只收本科：第一学历须为本科；最高学历为硕士或博士的直接淘汰。"
    assert profile["card_prescreen"]["degree_exact"] == "本科"
    assert profile["boss_filter"]["school_type"] == "公办本科"
    assert "degree" not in profile["boss_filter"]
    requirements = profile["additional_requirements"]
    outcomes = profile["resume_analysis"]["recent_outcomes"]
    assert "至少有一段工作经历具备至少2项数据成果" in requirements
    assert "量词（名量词、动量词、产出量词）、日期、0-1或金额" in requirements
    assert "具备数据分析能力" not in requirements
    assert "发现问题→分析原因→提出方案→量化结果" not in requirements
    assert "【加分项" not in requirements
    assert "管理经验" not in requirements
    assert "创业或0-1项目经验" not in requirements
    assert "自动化工具开发经验" not in requirements
    assert "多平台运营经验" not in requirements
    assert outcomes["distinct_metrics"] is False
    assert outcomes["exclude_money"] is True


def test_operation_requires_recent_competitor_operation_experience():
    profile = ConfigManager().load_job_profile("运营")
    card = profile["card_prescreen"]

    assert card["competitor_position_keywords"] == ["京东运营", "天猫运营", "运营"]
    assert card["competitor_recent_years"] == 3
    assert "国内运营" not in card["competitor_position_keywords"]
    assert "国内电商运营" not in card["competitor_position_keywords"]
    assert "结束/离职时间距今不超过3年" in profile["experience_required"]
    assert "京东运营/天猫运营/运营" in profile["additional_requirements"]


def test_amazon_boss_filter_uses_panel_degree_rules_and_new_experience_ranges():
    profile = ConfigManager().load_job_profile("亚马逊运营")

    assert profile["boss_filter"]["experience_years"] == ["1年以内", "1-3年"]
    assert profile["boss_filter"]["school_type"] == "公办本科"
    assert profile["boss_filter"]["first_degree_only"] is True
    assert "degree" not in profile["boss_filter"]
    # 学历交给 BOSS 面板，AI 不再判断学历。
    assert "degree_required" not in profile
    assert profile["card_prescreen"]["recent_work_min_months"] == 8
    platform = profile["resume_analysis"]["platform_experience"]
    assert platform["min_months"] == 8
    assert platform["regions"] == ["北美", "美国", "加拿大", "墨西哥", "美元", "美金", "$"]
    assert platform["market_match_in_content"] is True
    assert platform["fallback"] == {
        "self_intro": True,
        "recent_works": 2,
        "allow_missing_platform": True,
        "allow_missing_region": True,
    }
