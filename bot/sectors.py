"""Sector tags (backend, embedded, DevOps, AI/ML, …) for filtering the full list.

Deterministic: read from the job title first. Only when the title says nothing specific
("Software Engineer Intern") is the description used, and then a sector needs at least
three distinct keywords so boilerplate ("we value security") doesn't count.
"""
from __future__ import annotations

import re

from .textutil import fold

# key -> (emoji, label). Order = order of the filter buttons.
SECTORS: dict[str, tuple[str, str]] = {
    "backend": ("⚙️", "Backend"),
    "frontend": ("🎨", "Frontend"),
    "fullstack": ("🧩", "Full-stack"),
    "web": ("🌐", "Web"),
    "mobile": ("📱", "Mobile"),
    "embedded": ("🔌", "Embarqué / Firmware"),
    "systems": ("🖥️", "Systèmes / Bas niveau"),
    "devops": ("☁️", "DevOps / Cloud / Infra"),
    "ai": ("🤖", "IA / ML"),
    "data": ("📊", "Données"),
    "security": ("🔒", "Cybersécurité"),
    "qa": ("🧪", "QA / Tests"),
    "games": ("🎮", "Jeux vidéo / Graphisme"),
    "robotics": ("🦾", "Robotique / Autonome"),
    "quant": ("📈", "Finance / Trading"),
    "general": ("💻", "Logiciel (général)"),
}

_TITLE_RULES: dict[str, str] = {
    "backend": r"\bback[- ]?end\b|\bserver[- ]side\b|\bapis?\b|\bmicroservices?\b|\bdistributed systems?\b|"
               r"\b(java|\.net|c#|golang|node(\.js)?|ruby|php) (developer|engineer|programmer)\b|\bserveur\b",
    "frontend": r"\bfront[- ]?end\b|\bui (developer|engineer)\b|\bui/ux (developer|engineer)\b|\breact\b(?! native)|"
                r"\bangular\b|\bvue(\.js)?\b|\binterface utilisateur\b|\binterfaces? web\b",
    "fullstack": r"\bfull[- ]?stack\b|\bpleine pile\b",
    "web": r"\bweb\b",
    "mobile": r"\bmobile\b|\bios\b|\bandroid\b|\bflutter\b|\breact native\b",
    "embedded": r"\bembedded\b|\bembarque|\bfirmware\b|\bmicrologiciel|\bmicro-?controll?er|\biot\b|\brtos\b|"
                r"\bbare[- ]metal\b|\bfpga\b",
    "systems": r"\bkernel\b|\boperating systems?\b|\bsystemes? d'exploitation\b|\bsystems? (software|programming|programmer)\b|"
               r"\bcompilers?\b|\bdrivers?\b|\blow[- ]level\b|\bbas niveau\b|\bperformance (engineer|engineering)\b|"
               r"\bhpc\b|\bhigh[- ]performance\b|\bgpu\b|\bcuda\b|\breal[- ]time\b|\blinux\b",
    "devops": r"\bdev ?ops\b|\bdevsecops\b|\bsre\b|\bsite reliability\b|\bcloud\b|\binfrastructure\b|\binfra\b|"
              r"\bplatform\b|\bplateforme\b|\bbuild (engineer|systems?)\b|\brelease engineer|\bkubernetes\b|"
              r"\bnetwork(ing|s)?\b|\breseaux?\b|\bproduction engineer|\btools engineer|"
              r"\bdeveloper (productivity|experience|tools)\b",
    "ai": r"\bai\b|\bia\b|\bml\b|\bmachine learning\b|\bdeep learning\b|\bartificial intelligence\b|"
          r"\bintelligence artificielle\b|\bapprentissage (automatique|profond)\b|\bllms?\b|\bgen ?ai\b|"
          r"\bgenerative\b|\bnlp\b|\bcomputer vision\b|\bvision par ordinateur\b|\bmlops\b|\bneural\b",
    "data": r"\bdata\b(?!\s*cent(er|re))|\bdonnees\b|\banalytics\b|\betl\b|\bdatabases?\b|\bbases? de donnees\b",
    "security": r"\bsecurity\b|\bsecurite\b|\bcyber\w*|\bappsec\b|\bpentest\w*|\bpenetration\b|\bthreat\b|"
                r"\bcryptograph\w*|\bvulnerab\w*",
    "qa": r"\bqa\b|\bquality (assurance|engineer\w*)\b|\bassurance qualite\b|\btest(s|ing|eur|euse)?\b|\bsdet\b|"
          r"\bautomatisation des tests\b",
    "games": r"\bgames?\b|\bgaming\b|\bgameplay\b|\bjeux?\b|\bunreal\b|\bunity\b|\bgraphics\b|\brendering\b|"
             r"\bgame engine\b|\bmoteur de jeu\b|\b3d\b|\bgraphiques?\b",
    "robotics": r"\brobot\w*|\bautonom(y|ous)\b|\bautonome\b|\bself[- ]driving\b|\bdrones?\b|\bperception\b|\buav\b|"
                r"\bmotion planning\b",
    "quant": r"\bquant(itative)?\b|\btrading\b|\btrader\b|\balgo(rithmic)? trading\b|\bhft\b|\bmarket making\b",
}
_TITLE_RX = {k: re.compile(v) for k, v in _TITLE_RULES.items()}

# Trading firms whose generic "Software Developer Intern" is still a trading-systems role.
_QUANT_COMPANIES = re.compile(
    r"\b(tower research|capital de recherche tower|drw|citadel|jane street|hudson river|optiver|imc|virtu|"
    r"two sigma|jump trading|susquehanna|sig|five rings|akuna|old mission|point72|millennium|de shaw|"
    r"d\. e\. shaw|maven securities|flow traders|qube|xtx|radix|headlands)\b")

_DESC_KEYWORDS: dict[str, list[str]] = {
    "backend": ["backend", "back-end", "microservice", "rest api", "restful", "spring boot", "django", "flask",
                "fastapi", "node.js", "distributed system", "server-side", "grpc", "graphql", "api design"],
    "frontend": ["frontend", "front-end", "react", "angular", "vue", "typescript", "css", "html", "user interface",
                 "next.js", "tailwind", "ui component"],
    "mobile": ["ios", "android", "swift", "kotlin", "flutter", "react native", "mobile app"],
    "embedded": ["embedded", "firmware", "microcontroller", "rtos", "bare-metal", "bare metal", "embarque", "fpga",
                 "arm cortex", "iot", "i2c", "spi", "uart", "micrologiciel"],
    "systems": ["kernel", "operating system", "compiler", "llvm", "driver", "low-level", "low level", "cuda", "gpu",
                "linux internals", "memory management", "performance optimization", "multithreading"],
    "devops": ["kubernetes", "terraform", "ci/cd", "devops", "infrastructure as code", "site reliability",
               "observability", "helm", "ansible", "cloud infrastructure", "prometheus"],
    "ai": ["machine learning", "deep learning", "llm", "large language model", "pytorch", "tensorflow",
           "neural network", "computer vision", "nlp", "generative ai", "model training", "reinforcement learning"],
    "data": ["data pipeline", "etl", "spark", "airflow", "data warehouse", "dbt", "kafka", "data engineering",
             "bigquery", "data modeling", "hadoop"],
    "security": ["security engineer", "vulnerability", "vulnerabilities", "threat", "penetration test",
                 "cryptography", "cryptographic", "siem",
                 "incident response", "malware", "appsec", "secure coding"],
    "qa": ["test automation", "selenium", "cypress", "playwright", "quality assurance", "test cases",
           "regression testing", "test plans"],
    "games": ["game engine", "unreal", "unity", "gameplay", "rendering", "shader", "video game", "jeu video"],
    "robotics": ["robot", "ros", "autonomous", "motion planning", "slam", "lidar", "perception"],
}
# whole words, plurals allowed ("microservices", "REST APIs")
_DESC_RX = {k: [re.compile(r"(?<![a-z0-9])" + re.escape(w) + r"(?:s|es)?(?![a-z0-9])") for w in words]
            for k, words in _DESC_KEYWORDS.items()}
DESC_MIN_HITS = 3


def sectors_for(title: str | None, description: str | None = None, company: str | None = None) -> list[str]:
    """Sector keys for a posting, in SECTORS order; ['general'] when nothing specific is found."""
    t = fold(title)
    found = {k for k, rx in _TITLE_RX.items() if rx.search(t)}
    if company and _QUANT_COMPANIES.search(fold(company)):
        found.add("quant")
    if found & {"frontend", "fullstack"}:
        found.add("web")
    if not found - {"quant"} and description:
        d = fold(description)[:8000]
        hits = {k: sum(1 for rx in rxs if rx.search(d)) for k, rxs in _DESC_RX.items()}
        best = sorted((n, k) for k, n in hits.items() if n >= DESC_MIN_HITS)[::-1][:2]
        found |= {k for _, k in best}
        if found & {"frontend"} and found & {"backend"}:
            found |= {"fullstack", "web"}
        elif "frontend" in found:
            found.add("web")
    return [k for k in SECTORS if k in found] or ["general"]


def sector_label(key: str, emoji: bool = True) -> str:
    e, label = SECTORS.get(key, ("", key))
    return f"{e} {label}" if emoji else label
