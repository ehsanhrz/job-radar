"""Deterministic, explainable technical matching (evaluation schema v2).

This is rule-based extraction, not semantic or AI matching. Evidence always points
to literal source sentences, so operators can inspect uncertain classifications.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .filters import _compile
from .models import Job

log = logging.getLogger(__name__)
EVALUATION_VERSION = 2


@dataclass
class Requirement:
    category: str  # mandatory | preferred | optional | responsibility | environment | not_required
    skills: list[str]
    text: str
    evidence: str
    alternatives: bool = False
    weight: float = 1.0


@dataclass
class SkillMatch:
    requirement: str
    status: str
    category: str
    candidate_skills: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    reasoning: str = ""
    candidate_evidence: list[str] = field(default_factory=list)


@dataclass
class TechnicalEvaluation:
    technical_fit_score: int
    confidence: float
    role_family: str
    secondary_roles: list[str]
    role_confidence: float
    seniority: str
    seniority_confidence: float
    decision: str
    matched_required_skills: list[dict]
    missing_required_skills: list[dict]
    matched_preferred_skills: list[dict]
    missing_preferred_skills: list[dict]
    matched_optional_skills: list[dict]
    missing_optional_skills: list[dict]
    transferable_skills: list[dict]
    unknown_requirements: list[dict]
    requirements: list[dict]
    responsibilities: list[dict]
    important_gaps: list[str]
    scoring_breakdown: dict[str, Any]
    evidence: list[dict]
    reasons: list[str]
    rejection_reasons: list[str]
    candidate_profile_version: int = 1
    evaluation_version: int = EVALUATION_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


ROLE_PATTERNS: dict[str, tuple[str, ...]] = {
    "backend_engineering": (
        r"\bbackend\b|server[- ]side|backend apis?|application services|api development",
        r"(?:build|develop|implement).{0,35}(apis?|services|integrations)|message processing|distributed systems?",
        r"api engineer|integration engineer|product engineer|application engineer",
    ),
    "full_stack_engineering": (r"full[- ]stack|frontend.{0,35}backend|backend.{0,35}frontend",),
    "frontend_engineering": (r"frontend|front[- ]end|react|angular|vue|web ui|design systems?",),
    "mobile_engineering": (r"\bmobile\b|ios|android|swift|kotlin.{0,20}mobile|react native|flutter",),
    "platform_engineering": (r"platform engineer|developer platform|internal platform|infrastructure platform",
                             r"kubernetes|terraform|cloud infrastructure|deployment platform"),
    "devops": (r"\bdevops\b|infrastructure as code|build and release pipelines|cloud operations",),
    "sre": (r"\bsre\b|site reliability|incident response|on[- ]call|service reliability|production reliability",
            r"error budgets?|service level objectives?|observability platform"),
    "qa_testing": (r"\bqa\b|quality assurance|test automation|\bsdet\b|software tester",),
    "data_engineering": (r"data engineer|data platform|etl pipelines?|analytics infrastructure|data warehouse",),
    "machine_learning": (r"machine learning|\bml engineer\b|\bai engineer\b|model training|mlops",),
    "engineering_management": (r"engineering manager|manage.{0,20}(engineers|engineering team)|people management",
                                r"hiring|performance reviews|career development of engineers"),
    "other": (r"\btechnical support\b|sales engineer|solutions consultant|customer success",),
}

DEFAULT_ROLE_COMPATIBILITY = {
    "backend_engineering": 100, "full_stack_engineering": 68, "frontend_engineering": 18,
    "mobile_engineering": 12, "platform_engineering": 32, "devops": 20, "sre": 20,
    "qa_testing": 12, "data_engineering": 30, "machine_learning": 12,
    "engineering_management": 18, "other": 25, "unknown": 50,
}

SENIORITY_TITLE = {
    "internship": r"intern|internship|trainee|graduate program",
    "entry_level": r"entry[- ]level|new grad|graduate engineer",
    "junior": r"\bjunior\b|\bjr\.?\b",
    "mid_level": r"\bmid[- ]level\b|intermediate",
    "senior": r"\bsenior\b|\bsr\.?\b",
    "staff": r"\bstaff\b",
    "principal": r"\bprincipal\b",
    "lead": r"\btech(?:nical)? lead\b|\blead\b",
    "manager": r"\bengineering manager\b|\bengineering management\b",
    "director": r"\bdirector\b|\bvp of engineering\b",
}

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+|\s+[•●▪]\s+")
REQUIRED_CUE = re.compile(r"\b(required|must|minimum|need to have|essential|you have|we require)\b", re.I)
PREFERRED_CUE = re.compile(r"\b(preferred|desirable|desired|ideally|would be an advantage|we'd like|we would like)\b", re.I)
OPTIONAL_CUE = re.compile(r"\b(nice to have|nice-to-have|a plus|bonus|optional|helpful but not required)\b", re.I)
NOT_REQUIRED_CUE = re.compile(r"\b(not required|not needed|not necessary|not mandatory|does not require|no need to|no .* required|without .* experience)\b", re.I)
RESPONSIBILITY_CUE = re.compile(r"\b(build|design|develop|implement|maintain|operate|own|integrate|deliver|architect|scale|debug|support|lead|manage|hire|collaborate|responsible for|you will|on-call|incident response)\b", re.I)
ENVIRONMENT_CUE = re.compile(r"\b(our|the company.s|our infrastructure|our platform|we run|we use|built on|environment includes)\b", re.I)
HANDS_ON_CUE = re.compile(r"\b(hands[- ]on|production ownership|deep experience|extensive|strong experience|own.{0,15}production)\b", re.I)
YEARS_CUE = re.compile(r"(?:at least|minimum of|\bmin\.?|over|more than)?\s*(\d+)\+?\s+years?\b", re.I)

TRANSFER_MAP = {
    "Kafka": (("RabbitMQ", "Distributed systems"), "Messaging and distributed processing concepts transfer; Kafka-specific operations are unproven."),
    "RabbitMQ": (("Kafka", "Distributed systems"), "Messaging and distributed processing concepts transfer; RabbitMQ-specific operations are unproven."),
    "Kubernetes": (("Docker Swarm", "Docker", "Linux"), "Container and Linux experience transfers partially; Kubernetes production experience is not claimed."),
    "Terraform": (("Docker", "Linux", "CI/CD"), "Deployment and Linux experience is adjacent; infrastructure-as-code experience is not established."),
    "AWS": (("Docker", "Linux", "OpenTelemetry", "Prometheus"), "Application and operations experience is adjacent; AWS production ownership is not established."),
    "Azure": (("Docker", "Linux"), "Application and operations experience is adjacent; Azure production experience is not established."),
    "GCP": (("Docker", "Linux"), "Application and operations experience is adjacent; GCP production experience is not established."),
    "React": (("TypeScript",), "TypeScript experience transfers partially; React frontend depth is not established."),
    "SQL Server": (("PostgreSQL", "SQL"), "Relational database experience transfers; engine-specific expertise is partial."),
    "PostgreSQL": (("SQL Server", "SQL"), "Relational database experience transfers; engine-specific expertise is partial."),
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+#.]+", " ", (s or "").lower()).strip()


def _sentence_list(text: str) -> list[str]:
    return [s.strip(" \t-•") for s in SENTENCE_SPLIT.split(text or "") if len(s.strip()) >= 12]


class CandidateProfile:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.version = int(data.get("version", 1))
        self.identity = data.get("identity") or {}
        self.skills = data.get("skills") or []
        self.practices = data.get("practices") or []
        self.domains = data.get("domains") or []
        self._skill_aliases: list[tuple[str, str, str]] = []
        for item in self.skills:
            for phrase in [item["name"], *(item.get("aliases") or [])]:
                self._skill_aliases.append((phrase, item["name"], item.get("level", "additional")))
        self._practice_aliases = self._aliases(self.practices)
        self._domain_aliases = self._aliases(self.domains)

    @staticmethod
    def _aliases(items: list[dict]) -> list[tuple[str, str]]:
        return [(phrase, item["name"]) for item in items
                for phrase in [item["name"], *(item.get("aliases") or [])]]

    @classmethod
    def load(cls, path: str | Path) -> "CandidateProfile":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(data)

    def skill_mentions(self, sentence: str) -> list[str]:
        found = []
        low = sentence.lower()
        for phrase, canonical, _ in self._skill_aliases:
            phrase_hit = bool(re.search(r"\bGo\b", sentence)) if phrase == "Go" else _phrase_in(phrase, low)
            if phrase_hit and canonical not in found:
                found.append(canonical)
        for phrase, canonical in EXTERNAL_SKILL_ALIASES:
            if _phrase_in(phrase, low) and canonical not in found:
                found.append(canonical)
        if "SQL" in found and any(s in found for s in ("SQL Server", "PostgreSQL")):
            found.remove("SQL")  # avoid turning one engine mention into a generic extra requirement
        return found


EXTERNAL_SKILL_ALIASES = [
    ("aws", "AWS"), ("amazon web services", "AWS"), ("azure", "Azure"), ("gcp", "GCP"),
    ("google cloud", "GCP"), ("kubernetes", "Kubernetes"), ("k8s", "Kubernetes"),
    ("terraform", "Terraform"), ("react", "React"), ("javascript", "JavaScript"),
    ("vue", "Vue"), ("angular", "Angular"), ("swift", "Swift"), ("kotlin", "Kotlin"),
    ("incident response", "Incident Response"), ("on-call", "On-call"),
    ("infrastructure as code", "Infrastructure as Code"), ("system design", "System Design"),
    ("sql", "SQL"), ("rabbitmq", "RabbitMQ"), ("kafka", "Kafka"), ("postgresql", "PostgreSQL"),
    ("sql server", "SQL Server"), ("redis", "Redis"), ("docker", "Docker"), ("linux", "Linux"),
    ("microservices", "Microservices"), ("rest api", "REST APIs"), ("restful api", "REST APIs"),
    ("mysql", "MySQL"), ("mariadb", "MariaDB"), ("sqlite", "SQLite"), ("dynamodb", "DynamoDB"),
    ("mongodb", "MongoDB"), ("elasticsearch", "Elasticsearch"), ("cassandra", "Cassandra"),
    ("graphql", "GraphQL"), ("grpc", "gRPC"), ("soap", "SOAP"), ("websocket", "WebSockets"),
    ("java", "Java"), ("php", "PHP"), ("ruby", "Ruby"), ("rust", "Rust"), ("scala", "Scala"),
    ("kotlin", "Kotlin"), ("swift", "Swift"), ("c++", "C++"), ("node.js", "Node.js"),
    ("jenkins", "Jenkins"), ("github actions", "GitHub Actions"), ("gitlab ci", "GitLab CI"),
    ("ansible", "Ansible"), ("helm", "Helm"), ("eks", "Amazon EKS"), ("ecs", "Amazon ECS"),
    ("react", "React"), ("vue", "Vue"), ("angular", "Angular"), ("next.js", "Next.js"),
]


def _phrase_in(phrase: str, text: str) -> bool:
    phrase = phrase.strip().lower()
    if not phrase:
        return False
    if re.fullmatch(r"[a-z0-9+#. ]+", phrase):
        return re.search(r"(?<![\w+#])" + re.escape(phrase) + r"(?![\w+#])", text, re.I) is not None
    return phrase in text


class RoleClassifier:
    """Scores title and responsibility evidence separately and retains ambiguity."""

    def classify(self, job: Job) -> dict[str, Any]:
        title = job.title or ""
        description = job.description or ""
        sentences = _sentence_list(description)
        responsibility_sentences = [s for s in sentences if RESPONSIBILITY_CUE.search(s)]
        responsibility_text = " ".join(responsibility_sentences) or description
        scores: dict[str, float] = {}
        evidence: dict[str, list[str]] = {}
        description_hits: set[str] = set()
        for family, patterns in ROLE_PATTERNS.items():
            score = 4.0 if family == "full_stack_engineering" and re.search(r"full[- ]stack", title, re.I) else 0.0
            hits: list[str] = []
            for raw in patterns:
                rx = re.compile(raw, re.I)
                if rx.search(title):
                    score += 2.0
                    hits.append(title)
                role_sentences = [sentence for sentence in responsibility_sentences if rx.search(sentence)]
                if role_sentences:
                    score += 1.5
                    hits.extend(role_sentences[:2])
                    description_hits.add(family)
                # When postings have prose but no recognizable responsibility verbs,
                # description evidence still contributes at lower strength.
                if not responsibility_sentences and rx.search(description):
                    score += 0.8
                    hits.append(description[:220])
                    description_hits.add(family)
            if score:
                scores[family] = score
                evidence[family] = list(dict.fromkeys(hits))[:4]
        # SRE / platform / DevOps responsibilities outrank generic backend vocabulary.
        for infra in ("sre", "platform_engineering", "devops"):
            if scores.get(infra, 0) >= 3 and scores.get("backend_engineering", 0) < scores[infra] + 2:
                scores["backend_engineering"] = max(0, scores.get("backend_engineering", 0) - 2)
        if not scores:
            return {"primary": "unknown", "secondary": [], "confidence": 0.15,
                    "scores": {}, "evidence": [], "responsibility_sentences": responsibility_sentences}
        ordered = sorted(scores, key=lambda x: (-scores[x], x))
        primary = ordered[0]
        top, second = scores[primary], scores[ordered[1]] if len(ordered) > 1 else 0
        confidence = max(0.2, min(0.96, 0.45 + top / (top + second + 4) * 0.45 + min(top, 6) * 0.025))
        if primary not in description_hits:
            confidence = min(confidence, 0.48)
        if not description and primary == "backend_engineering" and not re.search(r"backend|server[- ]side|api", title, re.I):
            primary = "unknown"
            confidence = min(confidence, 0.38)
        if top < 1.5 or (top - second < 0.5 and top < 4.0):
            primary = "unknown"
            confidence = min(confidence, 0.4)
        secondaries = [r for r in ordered if r != primary and scores[r] >= max(2, top * 0.45)][:2]
        ev = [{"role": r, "snippet": s} for r in ([primary] if primary != "unknown" else []) + secondaries
              for s in evidence.get(r, [])]
        if primary == "backend_engineering" and re.search(r"integration|financial|payment|external service", description, re.I):
            if "integration_engineering" not in secondaries:
                secondaries.append("integration_engineering")
        if primary == "full_stack_engineering" and "backend_engineering" not in secondaries and scores.get("backend_engineering", 0) >= 2:
            secondaries.append("backend_engineering")
        return {"primary": primary, "secondary": secondaries, "confidence": round(confidence, 3),
                "scores": scores, "evidence": ev, "responsibility_sentences": responsibility_sentences}


class RequirementExtractor:
    def __init__(self, profile: CandidateProfile):
        self.profile = profile

    def extract(self, description: str) -> tuple[list[Requirement], list[str], list[str]]:
        requirements: list[Requirement] = []
        responsibilities: list[str] = []
        evidence: list[str] = []
        seen: dict[tuple[str, tuple[str, ...]], Requirement] = {}
        for sentence in _sentence_list(description):
            skills = self.profile.skill_mentions(sentence)
            # Candidate practices and domain facts are matched in their own dimensions.
            cat = ""
            if NOT_REQUIRED_CUE.search(sentence):
                cat = "not_required"
            elif OPTIONAL_CUE.search(sentence):
                cat = "optional"
            elif PREFERRED_CUE.search(sentence):
                cat = "preferred"
            elif REQUIRED_CUE.search(sentence):
                cat = "mandatory"
            elif RESPONSIBILITY_CUE.search(sentence):
                cat = "responsibility"
            elif ENVIRONMENT_CUE.search(sentence):
                cat = "environment"
            elif skills:
                cat = "environment"  # mention alone is not a candidate requirement

            if RESPONSIBILITY_CUE.search(sentence):
                responsibilities.append(sentence)
            if cat and (skills or cat in {"responsibility", "environment"}):
                evidence.append(sentence)
                if skills:
                    # The OR group is one criterion. For AND language, keep individual
                    # skill criteria so every mandatory skill contributes independently.
                    slash_alternatives = bool(re.search(
                        r"\b(?:AWS|Azure|GCP|PostgreSQL|SQL Server|RabbitMQ|Kafka)\s*/\s*(?:AWS|Azure|GCP|PostgreSQL|SQL Server|RabbitMQ|Kafka)\b",
                        sentence, re.I))
                    alternatives = len(skills) > 1 and bool(re.search(r"\bor\b|\beither\b|\bone of\b", sentence, re.I) or slash_alternatives)
                    groups = [skills] if alternatives else [[skill] for skill in skills]
                    weight = 1.4 if cat == "mandatory" and HANDS_ON_CUE.search(sentence) else 1.0
                    for group in groups:
                        key = (cat, tuple(sorted(group)))
                        previous = seen.get(key)
                        if not previous:
                            req = Requirement(cat, group, sentence[:500], sentence[:500], alternatives, weight)
                            seen[key] = req
                            requirements.append(req)
            elif REQUIRED_CUE.search(sentence):
                log.info("requirement extraction uncertain; no recognized skill in requirement sentence")
                requirements.append(Requirement("mandatory", [], sentence[:500], sentence[:500]))
        return requirements, list(dict.fromkeys(responsibilities)), list(dict.fromkeys(evidence))


class SkillMatcher:
    def __init__(self, profile: CandidateProfile):
        self.profile = profile
        self.by_name = {s["name"]: s for s in profile.skills}

    def match(self, req: Requirement) -> SkillMatch:
        if req.category == "not_required":
            return SkillMatch(" or ".join(req.skills), "not_applicable", req.category,
                              evidence=[req.evidence], reasoning="The posting explicitly says this is not required.")
        if req.category == "environment":
            return SkillMatch(" or ".join(req.skills), "not_applicable", req.category,
                              evidence=[req.evidence], reasoning="This is an employer-environment mention, not a stated candidate requirement.")
        if not req.skills:
            return SkillMatch(req.text, "unknown", req.category, evidence=[req.evidence],
                              reasoning="A requirement cue was found, but no known skill could be extracted.")
        direct: list[tuple[str, str]] = []
        transfer: list[tuple[str, str]] = []
        partial: list[str] = []
        for skill in req.skills:
            item = self.by_name.get(skill)
            if item:
                level = item.get("level", "additional")
                if level == "primary":
                    direct.append((skill, "strong_match"))
                elif level == "strong":
                    direct.append((skill, "match"))
                elif level == "additional":
                    direct.append((skill, "partial_match"))
                else:
                    partial.append(skill)
            elif skill in TRANSFER_MAP:
                candidates, why = TRANSFER_MAP[skill]
                if any(c in self.by_name for c in candidates):
                    transfer.append((skill, why))
        if direct:
            best = min((s for _, s in direct), key={"strong_match": 0, "match": 1, "partial_match": 2}.get)
            candidate = [name for name, status in direct if status == best]
            evidence = [self.by_name[n].get("evidence", "") for n in candidate]
            reasoning = {"strong_match": "Direct primary expertise is documented.",
                         "match": "Direct professional experience is documented.",
                         "partial_match": "The profile lists additional experience, not primary expertise."}[best]
            return SkillMatch(" or ".join(req.skills), best, req.category, candidate, [req.evidence],
                              reasoning, evidence)
        if transfer:
            direct_sources = list(dict.fromkeys(
                candidate for skill, _ in transfer for candidate in TRANSFER_MAP[skill][0]
                if candidate in self.by_name))
            candidate_evidence = [self.by_name[name].get("evidence", "") for name in direct_sources]
            return SkillMatch(" or ".join(req.skills), "transferable_match", req.category,
                              direct_sources, [req.evidence], " ".join(why for _, why in transfer), candidate_evidence)
        if partial:
            return SkillMatch(" or ".join(req.skills), "partial_match", req.category, partial, [req.evidence],
                              "Related profile evidence is limited and does not establish direct experience.")
        return SkillMatch(" or ".join(req.skills), "missing", req.category, [], [req.evidence],
                          "No direct or configured transferable experience is documented in the candidate profile.")


class SeniorityEvaluator:
    def __init__(self, candidate_years: float):
        self.candidate_years = candidate_years

    def evaluate(self, job: Job, description: str) -> dict[str, Any]:
        title = job.title or ""
        title_categories = [name for name, pattern in SENIORITY_TITLE.items() if re.search(pattern, title, re.I)]
        years = [int(x) for x in YEARS_CUE.findall(description or "")]
        year_level = None
        if years:
            n = max(years)
            year_level = "entry_level" if n <= 1 else "junior" if n <= 2 else "mid_level" if n <= 4 else "senior" if n <= 6 else "staff"
        if title_categories:
            level = title_categories[0]
            confidence = 0.88
            evidence = [title]
            if year_level and self._seniority_distance(level, year_level) >= 2:
                # Explicit years/responsibility expectations outweigh a misleading title.
                level = year_level
                confidence = 0.66
                evidence.append(next((s for s in _sentence_list(description) if YEARS_CUE.search(s)), ""))
        elif years:
            level = year_level or "unknown"
            confidence = 0.78
            evidence = [next((s for s in _sentence_list(description) if YEARS_CUE.search(s)), "")]
        elif re.search(r"architect|technical strategy|mentor|mentorship|team leadership", description or "", re.I):
            level, confidence, evidence = "lead", 0.5, ["Responsibilities imply elevated technical ownership."]
        elif re.search(r"own.{0,25}service|system design|technical design|architecture", description or "", re.I):
            level, confidence, evidence = "senior", 0.42, ["Responsibilities include service or system design ownership."]
        else:
            return {"level": "unknown", "confidence": 0.2, "fit": 65, "evidence": []}
        fit = self._fit(level, years)
        return {"level": level, "confidence": confidence, "fit": fit, "evidence": evidence}

    @staticmethod
    def _seniority_distance(a: str, b: str) -> int:
        order = ["internship", "entry_level", "junior", "mid_level", "senior", "lead", "staff", "principal", "director"]
        if a in {"manager", "director"} or b in {"manager", "director"}:
            return 0 if a == b else 3
        return abs(order.index(a) - order.index(b)) if a in order and b in order else 0

    def _fit(self, level: str, years: list[int]) -> int:
        if level in {"internship", "entry_level", "junior"}:
            return 38 if self.candidate_years >= 3 else 75
        if level in {"mid_level", "senior"}:
            return 92 if (not years or max(years) <= self.candidate_years + 2) else 62
        if level == "lead":
            return 72 if self.candidate_years >= 5 else 48
        if level in {"staff", "principal", "director", "manager"}:
            return 50 if level in {"manager", "director"} else 58
        return 65


DEFAULT_WEIGHTS = {"role": 0.25, "mandatory": 0.40, "preferred": 0.12,
                   "responsibilities": 0.10, "seniority": 0.08, "practices": 0.03, "domain": 0.02}
STATUS_VALUE = {"strong_match": 1.0, "match": 1.0, "transferable_match": 0.62,
                "partial_match": 0.42, "missing": 0.0, "unknown": 0.5, "not_applicable": 1.0}


class MatchingEngine:
    def __init__(self, cfg: dict, profile_path: str | Path | None = None):
        self.cfg = cfg
        mcfg = cfg.get("matching") or {}
        path = Path(profile_path or mcfg.get("candidate_profile", "candidate_profile.yaml"))
        self.profile = CandidateProfile.load(path)
        self.role_classifier = RoleClassifier()
        self.extractor = RequirementExtractor(self.profile)
        self.skill_matcher = SkillMatcher(self.profile)
        self.seniority_eval = SeniorityEvaluator(float(self.profile.identity.get("professional_experience_years", 0)))
        self.weights = {**DEFAULT_WEIGHTS, **{k: float(v) for k, v in (mcfg.get("weights") or {}).items()}}
        self.thresholds = {"strong_match": 80, "good_match": 65, "possible_match": 45,
                           **(mcfg.get("thresholds") or {})}
        self.min_score = int(self.thresholds["possible_match"])
        self.eligibility = _Eligibility(cfg)

    def title_ok(self, job: Job) -> bool:
        # Title is a discovery hint; full descriptions are needed to classify unusual titles.
        return True

    def evaluate(self, job: Job, today: date | None = None) -> TechnicalEvaluation:
        today = today or date.today()
        geo_reason = self.eligibility.reject_reason(job, today)
        role = self.role_classifier.classify(job)
        raw_reqs, responsibility_sentences, evidence = self.extractor.extract(job.description or "")
        # Practices and domains are extracted from the same literal sentence evidence.
        practice_matches = self._profile_matches(self.profile._practice_aliases, job.description or "")
        domain_matches = self._profile_matches(self.profile._domain_aliases, job.description or "")
        matches = [self.skill_matcher.match(r) for r in raw_reqs]
        required = [m for m in matches if m.category == "mandatory"]
        preferred = [m for m in matches if m.category == "preferred"]
        optional = [m for m in matches if m.category == "optional"]
        transferable = [m for m in matches if m.status == "transferable_match"]
        unknown = [m for m in matches if m.status == "unknown"]
        role_compat = {**DEFAULT_ROLE_COMPATIBILITY,
                       **((self.cfg.get("matching") or {}).get("role_compatibility") or {})}
        role_fit = int(role_compat.get(role["primary"], 50))
        required_fit, required_coverage = self._dimension(required, raw_reqs)
        preferred_fit, _ = self._dimension(preferred, raw_reqs)
        desc = job.description or ""
        resp_fit = self._responsibility_coverage(responsibility_sentences) if responsibility_sentences else (35 if desc else 25)
        seniority = self.seniority_eval.evaluate(job, desc)
        practice_fit = min(100, 45 + 22 * len(practice_matches)) if practice_matches else 48
        domain_fit = min(100, 40 + 30 * len(domain_matches)) if domain_matches else 42
        components = {"role": float(role_fit), "mandatory": required_fit, "preferred": preferred_fit,
                      "responsibilities": float(resp_fit), "seniority": float(seniority["fit"]),
                      "practices": float(practice_fit), "domain": float(domain_fit)}
        # A dimension with no extracted criteria is neutral, not evidence of a match.
        active = {"role", "responsibilities", "seniority"}
        if required:
            active.add("mandatory")
        if preferred:
            active.add("preferred")
        if practice_matches:
            active.add("practices")
        if domain_matches:
            active.add("domain")
        weight_sum = sum(self.weights[k] for k in active)
        score = round(sum(components[k] * self.weights[k] for k in active) / weight_sum) if weight_sum else 50
        # Critical absent platform ownership is a meaningful gap when explicitly mandatory.
        mcfg = self.cfg.get("matching") or {}
        critical_names = set(mcfg.get("critical_requirements") or
                             ("AWS", "Azure", "GCP", "Kubernetes", "Terraform"))
        critical_missing = [m for m in required if m.status in {"missing", "transferable_match", "partial_match"}
                           and any(s in critical_names for s in raw_reqs_for(m, raw_reqs))]
        critical_penalty = min(int(mcfg.get("critical_gap_penalty_max", 18)),
                               len(critical_missing) * int(mcfg.get("critical_gap_penalty_per_requirement", 8)))
        if critical_penalty:
            score -= critical_penalty
        score = max(0, min(100, int(score)))
        confidence, confidence_inputs = self._confidence(desc, raw_reqs, responsibility_sentences, role["confidence"])
        decision = self._decision(score, confidence)
        if geo_reason:
            decision = "ineligible"
        req_dicts = [asdict(r) for r in raw_reqs]
        details = [{"requirement": asdict(r), "match": asdict(m)} for r, m in zip(raw_reqs, matches)]
        matched_required = [asdict(m) for m in required if m.status in {"strong_match", "match", "partial_match"}]
        missing_required = [asdict(m) for m in required if m.status == "missing"]
        matched_preferred = [asdict(m) for m in preferred if m.status in {"strong_match", "match", "partial_match"}]
        missing_preferred = [asdict(m) for m in preferred if m.status == "missing"]
        matched_optional = [asdict(m) for m in optional if m.status in {"strong_match", "match", "partial_match", "transferable_match"}]
        missing_optional = [asdict(m) for m in optional if m.status == "missing"]
        gaps = [f"Mandatory: {m.requirement}" for m in required if m.status == "missing"]
        gaps.extend(f"Transferable only: {m.requirement}" for m in transferable if m.category == "mandatory")
        gaps.extend(f"Partial experience: {m.requirement}" for m in required if m.status == "partial_match")
        reasons = []
        if role["primary"] == "backend_engineering":
            reasons.append("Responsibilities indicate backend engineering.")
        reasons.extend(f"Direct match: {m.requirement}." for m in required
                       if m.status in {"strong_match", "match", "partial_match"})
        reasons.extend(f"Transferable: {m.requirement}." for m in transferable[:2])
        reasons.extend(gaps[:3])
        if not responsibility_sentences and not desc:
            reasons.append("Description is sparse; this is a low-confidence preliminary match.")
        rejection = [geo_reason] if geo_reason else (["Technical role is poorly aligned with the backend candidate profile."]
                   if decision == "poor_match" else [])
        full_evidence = [{"kind": "role", **ev} for ev in role["evidence"]]
        full_evidence += [{"kind": "requirement", "snippet": s} for s in evidence]
        full_evidence += [{"kind": "responsibility", "snippet": s} for s in responsibility_sentences]
        full_evidence += [{"kind": "candidate_profile", "snippet": s.get("evidence", ""), "skill": s["name"]}
                          for s in self.profile.skills if any(m.requirement == s["name"] for m in matches)]
        breakdown: dict[str, Any] = {"dimensions": components,
                                     "weights": {k: self.weights[k] for k in active},
                                     "active_weight_sum": round(weight_sum, 4),
                                     "confidence_inputs": confidence_inputs,
                                     "critical_missing_penalty": critical_penalty,
                                     "score": score}
        log.info("matching v%d role=%s seniority=%s score=%d confidence=%.2f decision=%s",
                 EVALUATION_VERSION, role["primary"], seniority["level"], score, confidence, decision)
        if role["primary"] == "unknown":
            log.info("matching role classification unknown for job %s", job.id)
        if confidence < 0.4:
            log.info("matching low confidence for job %s", job.id)
        if decision == "poor_match":
            log.info("job %s rejected for technical role mismatch", job.id)
        return TechnicalEvaluation(
            score, confidence, role["primary"], role["secondary"], role["confidence"],
            seniority["level"], seniority["confidence"], decision,
            matched_required, missing_required, matched_preferred, missing_preferred,
            matched_optional, missing_optional,
            [asdict(m) for m in transferable], [asdict(m) for m in unknown],
            details, [{"text": s, "evidence": s} for s in responsibility_sentences],
            list(dict.fromkeys(gaps)), breakdown,
            full_evidence, list(dict.fromkeys(reasons)), rejection,
            self.profile.version,
        )

    def _profile_matches(self, aliases: list[tuple[str, str]], text: str) -> list[dict]:
        out = []
        sentences = _sentence_list(text)
        for phrase, name in aliases:
            hits = [s for s in sentences if _phrase_in(phrase, s.lower())]
            if hits and name not in [m["name"] for m in out]:
                out.append({"name": name, "evidence": hits[:2]})
        return out

    @staticmethod
    def _responsibility_coverage(sentences: list[str]) -> int:
        buckets = {
            "build": r"build|develop|implement|deliver",
            "design": r"design|architect|system design",
            "integrate": r"integrat|external service|api",
            "operate": r"operate|maintain|support|debug|production",
            "scale": r"scale|performance|reliability|resilien",
            "collaborate": r"collaborat|mentor|lead|partner",
        }
        covered = sum(any(re.search(pattern, sentence, re.I) for sentence in sentences)
                      for pattern in buckets.values())
        return min(100, 40 + 12 * covered)

    def _dimension(self, items: list[SkillMatch], requirements: list[Requirement]) -> tuple[float, float]:
        if not items:
            return 50.0, 0.0
        known = [m for m in items if m.status != "unknown"]
        if not known:
            return 50.0, 0.0
        vals = [STATUS_VALUE[m.status] for m in known]
        req_weights = {" or ".join(r.skills) if r.skills else r.text: r.weight for r in requirements}
        weights = [req_weights.get(m.requirement, 1.0) for m in known]
        return round(100 * sum(v * w for v, w in zip(vals, weights)) / sum(weights), 2), len(known) / len(items)

    def _confidence(self, desc: str, requirements: list[Requirement], responsibilities: list[str],
                    role_conf: float) -> tuple[float, dict[str, Any]]:
        unique_description = " ".join(dict.fromkeys(_sentence_list(desc)))
        words = len(re.findall(r"\w+", unique_description))
        req_count = len(requirements)
        resp_count = len(responsibilities)
        unknown_penalty = 0.08 if any(r.category == "mandatory" and not r.skills for r in requirements) else 0
        value = 0.22 + min(words / 400, 0.28) + min(req_count / 8, 0.22)
        value += min(resp_count / 6, 0.16) + role_conf * 0.16
        if not desc:
            value = min(value, 0.25)
        value -= unknown_penalty
        factors = {"unique_description_words": words, "recognized_requirement_groups": req_count,
                   "responsibility_sentences": resp_count, "role_confidence": role_conf,
                   "unknown_requirement_penalty": unknown_penalty,
                   "no_description_cap_applied": not bool(desc)}
        return round(max(0.05, min(0.98, value)), 2), factors

    def _decision(self, score: int, confidence: float) -> str:
        if confidence < float((self.cfg.get("matching") or {}).get("insufficient_confidence", 0.25)):
            return "insufficient_information"
        if score >= int(self.thresholds["strong_match"]) and confidence >= 0.60:
            return "strong_match"
        if score >= int(self.thresholds["good_match"]):
            return "good_match"
        if score >= int(self.thresholds["possible_match"]):
            return "possible_match"
        return "poor_match"

    def status(self, evaluation: TechnicalEvaluation) -> tuple[str, str | None]:
        if evaluation.decision == "ineligible":
            return "filtered", "; ".join(evaluation.rejection_reasons)
        if evaluation.decision == "poor_match":
            return "filtered", "; ".join(evaluation.rejection_reasons) or "technical: poor match"
        if evaluation.decision in {"strong_match", "good_match"}:
            return "queued", None
        return "low", None


def raw_reqs_for(match: SkillMatch, requirements: list[Requirement]) -> list[str]:
    for req in requirements:
        if " or ".join(req.skills) == match.requirement:
            return req.skills
    return []


class _Eligibility:
    """Retains current geography/work-authorisation/remote/age policy outside fit."""
    def __init__(self, cfg: dict):
        filters = cfg.get("filters") or {}
        self.location_exclude = _compile(filters.get("location_exclude"))
        self.hard_exclude = _compile(filters.get("hard_exclude"))
        self.require_remote_sites = set(filters.get("require_remote_sites") or [])
        self.direct_apply_only_sites = set(filters.get("direct_apply_only_sites") or [])
        self.max_age_days = filters.get("max_age_days")

    def reject_reason(self, job: Job, today: date) -> str | None:
        if job.site in self.direct_apply_only_sites:
            from urllib.parse import urlsplit
            host = urlsplit(job.url or "").hostname or ""
            if job.site in host:
                return f"apply only via {job.site}"
        for pattern in self.location_exclude:
            if pattern.search(job.location or ""):
                return f"location: {(job.location or '')[:60]}"
        for pattern in self.hard_exclude:
            hit = pattern.search(job.text)
            if hit:
                return f"hard_exclude: {hit.group(0)[:60]}"
        if job.site in self.require_remote_sites and job.is_remote is False:
            return f"not remote ({job.site})"
        if self.max_age_days and job.date_posted and (today - job.date_posted).days > int(self.max_age_days):
            return "too old"
        return None
