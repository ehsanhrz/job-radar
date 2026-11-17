from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import yaml

from jobradar.config import Settings
from jobradar.db import DB
from jobradar.matching import EVALUATION_VERSION, MatchingEngine
from jobradar.models import Job
from jobradar.pipeline import ingest
from jobradar.collectors.base import CollectResult
from jobradar.telegram import format_job

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
ENGINE = MatchingEngine(CFG)


def job(title: str, description: str, **kwargs) -> Job:
    return Job(source="test", site="test", title=title, url=f"https://jobs.example/{abs(hash(title + description))}",
               description=description, company="Example", location="Remote: Worldwide", is_remote=True,
               **kwargs)


STRONG_DESCRIPTION = (
    "We are hiring a backend engineer to build and design production services for our financial products. "
    "You will develop C# and ASP.NET Core APIs, integrate payment and exchange providers, and operate distributed "
    "message processing with RabbitMQ. Required skills include C#, .NET, PostgreSQL, RabbitMQ, distributed systems, "
    "and external API integrations. Domain-Driven Design and CQRS guide the architecture. You will own service "
    "design, collaborate with product teams, maintain tests, and support production systems."
)


def test_strong_dotnet_backend_match_is_explainable():
    result = ENGINE.evaluate(job("Senior .NET Backend Engineer", STRONG_DESCRIPTION))
    assert result.role_family == "backend_engineering"
    assert result.seniority == "senior"
    assert result.technical_fit_score >= 80
    assert result.confidence >= 0.60
    assert result.decision == "strong_match"
    assert result.matched_required_skills
    assert "C#" in [m["candidate_skills"][0] for m in result.matched_required_skills]
    assert result.scoring_breakdown["weights"]["mandatory"] == 0.4
    assert result.evaluation_version == EVALUATION_VERSION
    assert all(e["snippet"] for e in result.evidence)


def test_integration_engineer_classifies_from_responsibilities():
    result = ENGINE.evaluate(job("Integration Engineer", (
        "You will build backend APIs for financial integrations, develop external service connections, "
        "and process messages in distributed .NET services. Required: C# and .NET."
    )))
    assert result.role_family == "backend_engineering"
    assert "integration_engineering" in result.secondary_roles
    assert result.technical_fit_score >= 65


def test_unusual_titles_reach_description_classification():
    for title in ("API Engineer", "Software Engineer", "Product Engineer", "Application Engineer",
                  "Systems Engineer", "Fintech Engineer", "Backend Platform Engineer", "Distributed Systems Engineer"):
        result = ENGINE.evaluate(job(title, "You will develop backend APIs and integrate external financial services in .NET."))
        assert result.role_family == "backend_engineering", (title, result.role_family)


def test_aws_preferred_does_not_veto_direct_backend_match():
    desc = STRONG_DESCRIPTION + " AWS production experience is preferred."
    result = ENGINE.evaluate(job("Senior .NET Backend Engineer", desc))
    aws = [m for m in result.requirements if m["requirement"]["skills"] == ["AWS"]]
    assert aws and aws[0]["requirement"]["category"] == "preferred"
    assert result.technical_fit_score >= 75
    assert result.decision in {"good_match", "strong_match"}
    assert not any("AWS" in m["requirement"] for m in result.missing_required_skills)


def test_mandatory_aws_ownership_is_a_material_gap():
    base = ENGINE.evaluate(job("Senior .NET Backend Engineer", STRONG_DESCRIPTION))
    cloud = ENGINE.evaluate(job("Senior .NET Backend Engineer", STRONG_DESCRIPTION +
                                 " Hands-on production ownership of AWS infrastructure is required."))
    assert any(m["requirement"] == "AWS" and m["status"] == "transferable_match"
               for m in cloud.transferable_skills)
    assert any(m["candidate_evidence"] for m in cloud.transferable_skills if m["requirement"] == "AWS")
    assert cloud.technical_fit_score < base.technical_fit_score
    assert cloud.important_gaps
    assert cloud.scoring_breakdown["critical_missing_penalty"] > 0


def test_sre_job_is_not_ranked_as_backend():
    result = ENGINE.evaluate(job("Site Reliability Engineer", (
        "You will own Kubernetes and Terraform infrastructure in AWS, lead production incident response, "
        "manage on-call reliability, service level objectives, and observability platforms. "
        "Required: Kubernetes, Terraform, AWS, and incident response."
    )))
    assert result.role_family == "sre"
    assert result.technical_fit_score < 65
    assert result.decision != "strong_match"


def test_full_stack_reports_missing_frontend_requirement():
    result = ENGINE.evaluate(job("Full Stack Engineer", (
        "You will build backend APIs in C# and .NET and develop customer interfaces in React. "
        "Required: C#, .NET, SQL Server, and strong React experience."
    )))
    assert result.role_family == "full_stack_engineering"
    assert any(m["requirement"] == "React" for m in result.transferable_skills)
    assert result.technical_fit_score < 100


def test_optional_kubernetes_has_no_fit_penalty():
    base = ENGINE.evaluate(job("Senior .NET Backend Engineer", STRONG_DESCRIPTION))
    optional = ENGINE.evaluate(job("Senior .NET Backend Engineer", STRONG_DESCRIPTION +
                                    " Kubernetes knowledge is a plus."))
    assert any(m["requirement"]["category"] == "optional" and m["match"]["status"] in {"missing", "transferable_match"}
               for m in optional.requirements if m["requirement"]["skills"] == ["Kubernetes"])
    assert optional.technical_fit_score == base.technical_fit_score


def test_negated_requirement_and_company_environment_are_not_gaps():
    result = ENGINE.evaluate(job("Senior .NET Backend Engineer", (
        "You will build backend APIs with C# and PostgreSQL. Kubernetes experience is not required. "
        "Our infrastructure runs on AWS."
    )))
    reqs = {tuple(item["requirement"]["skills"]): item for item in result.requirements}
    assert reqs[("Kubernetes",)]["requirement"]["category"] == "not_required"
    assert reqs[("Kubernetes",)]["match"]["status"] == "not_applicable"
    assert reqs[("AWS",)]["requirement"]["category"] == "environment"
    assert reqs[("AWS",)]["match"]["status"] == "not_applicable"
    assert "AWS" not in " ".join(result.important_gaps)


def test_alternative_relational_requirement_is_satisfied_by_either_database():
    result = ENGINE.evaluate(job("Backend Engineer", (
        "You will develop backend services. PostgreSQL or SQL Server experience is required."
    )))
    alternatives = [r for r in result.requirements if r["requirement"]["alternatives"]]
    assert alternatives
    assert alternatives[0]["match"]["status"] in {"strong_match", "match"}


def test_repeated_keyword_mentions_do_not_inflate_fit():
    once = ENGINE.evaluate(job("Senior .NET Backend Engineer", STRONG_DESCRIPTION))
    repeated = ENGINE.evaluate(job("Senior .NET Backend Engineer", STRONG_DESCRIPTION +
                                   (" Required: C#, .NET, PostgreSQL, RabbitMQ. " * 12)))
    assert repeated.technical_fit_score == once.technical_fit_score
    assert len([r for r in repeated.requirements if r["requirement"]["category"] == "mandatory"]) == \
           len([r for r in once.requirements if r["requirement"]["category"] == "mandatory"])


def test_sparse_description_has_low_confidence_without_invented_gaps():
    result = ENGINE.evaluate(job("Software Engineer", "Join our growing team."))
    assert result.confidence < 0.4
    assert result.role_family == "unknown"
    assert result.missing_required_skills == []
    assert result.unknown_requirements == []
    assert result.decision in {"insufficient_information", "possible_match"}


def test_frontend_mobile_and_management_roles_are_role_aware():
    frontend = ENGINE.evaluate(job("Software Engineer", "You will develop React interfaces, design web UI systems, and implement frontend components."))
    mobile = ENGINE.evaluate(job("Software Engineer", "You will build iOS and Android applications using Swift and Kotlin."))
    manager = ENGINE.evaluate(job("Engineering Manager", "You will manage engineers, hire staff, run performance reviews, and own career development."))
    assert frontend.role_family == "frontend_engineering" and frontend.technical_fit_score < 65
    assert mobile.role_family == "mobile_engineering" and mobile.technical_fit_score < 65
    assert manager.role_family == "engineering_management" and manager.technical_fit_score < 65


def test_seniority_categories_are_distinct_and_candidate_aware():
    cases = {"Internship Software Engineer": "internship", "Entry-level Backend Engineer": "entry_level",
             "Junior Backend Engineer": "junior", "Mid-level Backend Engineer": "mid_level",
             "Senior Backend Engineer": "senior", "Staff Backend Engineer": "staff",
             "Principal Backend Engineer": "principal", "Lead Backend Engineer": "lead",
             "Engineering Manager": "manager", "Director of Engineering": "director"}
    for title, expected in cases.items():
        result = ENGINE.evaluate(job(title, "You will develop backend APIs and services."))
        assert result.seniority == expected, (title, result.seniority)
    assert ENGINE.evaluate(job("Backend Engineer", "Build backend APIs." )).seniority == "unknown"
    misleading_title = ENGINE.evaluate(job("Senior Backend Engineer",
                                           "You will develop APIs. Candidates need 1 year of experience."))
    assert misleading_title.seniority == "entry_level"


def test_score_confidence_and_reproducibility_properties():
    sample = job("Senior .NET Backend Engineer", STRONG_DESCRIPTION)
    a, b = ENGINE.evaluate(sample), ENGINE.evaluate(sample)
    assert a.to_json() == b.to_json()
    assert "unique_description_words" in a.scoring_breakdown["confidence_inputs"]
    for title, desc in (("Software Engineer", ""), ("SRE", "Kubernetes Terraform AWS required."),
                        ("Backend Engineer", STRONG_DESCRIPTION)):
        result = ENGINE.evaluate(job(title, desc))
        assert 0 <= result.technical_fit_score <= 100
        assert 0 <= result.confidence <= 1
    empty = ENGINE.evaluate(job("Software Engineer", ""))
    assert empty.scoring_breakdown["confidence_inputs"]["no_description_cap_applied"]
    fewer = ENGINE.evaluate(job("Backend Engineer", "C# and .NET are required."))
    more_covered = ENGINE.evaluate(job("Backend Engineer", "C#, .NET, PostgreSQL and SQL Server are required."))
    assert more_covered.technical_fit_score >= fewer.technical_fit_score


def test_fit_display_is_compact_and_uses_v2_fields():
    row = {"title": "Senior Backend Engineer", "company": "Acme", "location": "Remote",
           "url": "https://example.test/job", "score": 500, "technical_fit_score": 82,
           "role_family": "backend_engineering", "seniority": "senior", "confidence": .78,
           "evaluation_details": json.dumps({"reasons": ["Direct match: C#", "Direct match: .NET"],
                                              "important_gaps": ["Mandatory: AWS"]}),
           "reasons": "[]", "is_remote": 1, "date_posted": "2026-01-01"}
    text = format_job(row)
    assert "Technical fit 82/100" in text and "confidence 78%" in text and "AWS" in text
    assert "500" not in text


def test_ingest_persists_v2_and_does_not_mix_legacy_score(tmp_path):
    db = DB(tmp_path / "v2.db")
    old = job("Legacy", "Old job")
    db.insert(old, "queued")
    db.conn.execute("UPDATE jobs SET score=947 WHERE id=?", (old.id,))
    db.conn.commit()
    new = job("Senior .NET Backend Engineer", STRONG_DESCRIPTION)
    result = CollectResult()
    result.jobs = [new]
    counts = ingest(db, ENGINE, result)
    row = db.get(new.id)
    assert counts["new"] == 1 and row["evaluation_version"] == 2
    assert 0 <= row["technical_fit_score"] <= 100
    assert row["evaluation_details"] and json.loads(row["evaluation_details"])["role_family"] == "backend_engineering"
    assert db.get(old.id)["score"] == 947 and db.get(old.id)["evaluation_version"] is None
    assert [r["id"] for r in db.queued(100, evaluation_version=2)] == [new.id]


def test_run_once_uses_v2_for_pipeline_and_telegram(tmp_path, monkeypatch):
    from jobradar import pipeline
    from jobradar.collectors import ats
    result = CollectResult()
    candidate = job("Senior .NET Backend Engineer", STRONG_DESCRIPTION)
    result.jobs = [candidate]
    result.call("synthetic", found=1)
    monkeypatch.setattr(pipeline, "collect_all", lambda *args, **kwargs: result)
    monkeypatch.setattr(ats, "run_step", lambda *args, **kwargs: CollectResult())
    monkeypatch.setattr(pipeline.time, "sleep", lambda _: None)

    class FakeTelegram:
        def __init__(self, *args, **kwargs):
            self.messages = []

        def send(self, text, buttons=None):
            self.messages.append((text, buttons))
            return len(self.messages)

    tg = FakeTelegram()
    monkeypatch.setattr(pipeline, "Telegram", lambda *args, **kwargs: tg)
    cfg = {**CFG, "telegram": {"max_per_run": 5, "send_run_summary": False}}
    settings = Settings(cfg, "token", "42", None, [], tmp_path / "run.db")
    summary = pipeline.run_once(settings, process_updates=False)
    stored = DB(settings.db_path).get(candidate.id)
    assert summary["sent"] == 1 and stored["status"] == "sent"
    assert stored["evaluation_version"] == 2
    assert "Technical fit" in tg.messages[0][0] and "backend_engineering" in tg.messages[0][0]
    assert tg.messages[0][1][0][0]["callback_data"].startswith("st:applied:")


def test_additive_migration_preserves_history_and_scores(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE jobs (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, source TEXT NOT NULL,
        site TEXT NOT NULL, target TEXT, title TEXT NOT NULL, company TEXT, location TEXT,
        url TEXT NOT NULL, is_remote INTEGER, date_posted TEXT, salary TEXT, description TEXT,
        score INTEGER NOT NULL DEFAULT 0, reasons TEXT, status TEXT NOT NULL, filter_reason TEXT,
        first_seen TEXT NOT NULL, sent_at TEXT, tg_message_id INTEGER, updated_at TEXT);
      CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
      INSERT INTO jobs (id,fingerprint,source,site,title,url,score,status,first_seen,sent_at,tg_message_id,updated_at)
        VALUES ('a','fa','test','board','Applied job','https://e/a',999,'applied','then','sent-time',123,'updated');
      INSERT INTO jobs (id,fingerprint,source,site,title,url,score,status,first_seen,updated_at)
        VALUES ('q','fq','test','board','Queued job','https://e/q',451,'queued','then','updated');
      INSERT INTO jobs (id,fingerprint,source,site,title,url,score,status,first_seen,updated_at)
        VALUES ('indeed','fi','test','indeed','Legacy Indeed job','https://indeed.com/view/1',317,'queued','then','updated');
    """)
    conn.commit()
    conn.close()
    DB(path)  # invokes only additive matching-column migration
    migrated = DB(path)
    applied, queued = migrated.get("a"), migrated.get("q")
    assert applied["status"] == "applied" and applied["score"] == 999
    assert applied["sent_at"] == "sent-time" and applied["tg_message_id"] == 123
    assert queued["status"] == "queued" and queued["score"] == 451
    assert migrated.get("indeed")["status"] == "queued" and migrated.get("indeed")["score"] == 317
    assert migrated.get_meta("cleanup_indeed_v1") == "superseded_preserve_history"
    assert {"evaluation_version", "technical_fit_score", "role_family", "seniority", "confidence",
            "match_decision", "evaluation_details"} <= {r["name"] for r in migrated.conn.execute("PRAGMA table_info(jobs)")}


def test_real_database_copy_migrates_without_modifying_history(tmp_path):
    source_path = ROOT / "data" / "jobradar.db"
    if not source_path.exists():
        import pytest
        pytest.skip("checkout has no local data/jobradar.db")
    copied = tmp_path / "jobradar-copy.db"
    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    destination = sqlite3.connect(copied)
    source.backup(destination)
    source.close()
    destination.close()
    before_conn = sqlite3.connect(copied)
    before = before_conn.execute(
        "SELECT id, fingerprint, url, score, status, sent_at, tg_message_id, updated_at FROM jobs ORDER BY id"
    ).fetchall()
    before_conn.close()
    migrated = DB(copied)
    after = [tuple(row) for row in migrated.conn.execute(
        "SELECT id, fingerprint, url, score, status, sent_at, tg_message_id, updated_at FROM jobs ORDER BY id"
    ).fetchall()]
    assert after == before


def test_pending_legacy_rows_upgrade_without_requeuing_filtered_or_history(tmp_path):
    from jobradar.pipeline import _upgrade_pending_evaluations
    db = DB(tmp_path / "pending.db")
    queued = job("Senior .NET Backend Engineer", STRONG_DESCRIPTION)
    low = job("Software Engineer", "Join our growing team.")
    sent = job("Sent", "Description")
    filtered = job("Filtered", "Must be based in the UK.")
    for item, status in ((queued, "queued"), (low, "low"), (sent, "sent"), (filtered, "filtered")):
        db.insert(item, status)
    db.conn.execute("UPDATE jobs SET score=555 WHERE id=?", (queued.id,))
    db.conn.commit()
    count = _upgrade_pending_evaluations(db, ENGINE)
    assert count == 2
    assert db.get(queued.id)["status"] == "queued" and db.get(queued.id)["score"] == 555
    assert db.get(sent.id)["evaluation_version"] is None and db.get(sent.id)["status"] == "sent"
    assert db.get(filtered.id)["evaluation_version"] is None and db.get(filtered.id)["status"] == "filtered"


def test_review_queue_lists_v2_low_and_borderline_jobs(tmp_path):
    from jobradar.bot import _review_page, handle_update
    db = DB(tmp_path / "review.db")
    items = [job(f"Software Engineer {i}", "Join our growing engineering team.") for i in range(12)]
    for item in items:
        evaluation = ENGINE.evaluate(item)
        status, reason = ENGINE.status(evaluation)
        db.insert(item, status, reason, evaluation.to_dict())
    text, buttons = _review_page(db, 1)
    assert "V2 review queue: 12" in text and "Showing 1–10" in text and buttons[0][-1]["callback_data"] == "rv:2"
    sent = []
    tg = type("TG", (), {"send": lambda self, body, keyboard=None: sent.append(body)})()
    settings = Settings({}, "t", "42", None, [], tmp_path / "review.db")
    handle_update(db, tg, settings, {"message": {"chat": {"id": 42}, "text": "/review 2"}})
    assert len(sent) == 1 and sent[0].count("Explain:") == 2

