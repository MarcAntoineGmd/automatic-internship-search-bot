"""Sector tags used by the HTML filter (backend, embedded, DevOps, AI/ML, …)."""
from __future__ import annotations

import pytest

from bot.sectors import SECTORS, sector_label, sectors_for


@pytest.mark.parametrize("title, expected", [
    ("Software Engineer Intern, Backend (Summer 2027 - Montreal)", ["backend"]),
    ("Développeur Logiciels (Stagiaire), Backend", ["backend"]),
    ("Frontend Developer Intern", ["frontend", "web"]),
    ("Front-End Web Development Intern", ["frontend", "web"]),
    ("Full Stack Developer Intern - C#", ["fullstack", "web"]),
    ("Web Software Developer Intern - WFM", ["web"]),
    ("Software Developer Intern - Android", ["mobile"]),
    ("Firmware Engineer Intern - Embedded Software Engineering", ["embedded"]),
    ("Stagiaire en systèmes embarqués", ["embedded"]),
    ("Machine Learning Compiler & Performance Engineering Intern", ["systems", "ai"]),
    ("Linux Kernel Intern", ["systems"]),
    ("Engineering Intern, DevOps", ["devops"]),
    ("Site Reliability Engineering Co-op", ["devops"]),
    ("Software Engineer Intern, Machine Learning", ["ai"]),
    ("Stagiaire en intelligence artificielle", ["ai"]),
    ("Data Engineer Intern", ["data"]),
    ("Student Cyber Security Analyst Intern", ["security"]),
    ("STAGE – INGÉNIEUR CYBERSÉCURITÉ & INTELLIGENCE ARTIFICIELLE", ["ai", "security"]),
    ("Software Development Engineer in Test Co-op", ["qa"]),
    ("Gameplay Software Engineer Co-op", ["games"]),
    ("Software Development Engineer Intern, Robotics", ["robotics"]),
    ("Software Developer Intern", ["general"]),
])
def test_sector_from_title(title, expected):
    assert sectors_for(title) == expected


def test_securities_and_data_centre_are_not_misread():
    assert sectors_for("Software Developer Intern - Global Securities") == ["general"]
    assert "data" not in sectors_for("AI Software Intern, Cloud, Infrastructure & Data Centre Deployment")


def test_trading_firm_generic_title_is_tagged_quant():
    assert sectors_for("Software Developer Intern", company="DRW") == ["quant"]
    assert sectors_for("Machine Learning Intern", company="Tower Research Capital") == ["ai", "quant"]


def test_description_used_only_for_generic_titles_and_needs_several_keywords():
    desc = "You will build microservices and REST APIs with Spring Boot and Kafka in a distributed system."
    assert sectors_for("Software Engineer Intern", desc) == ["backend"]
    # one passing mention is not enough
    assert sectors_for("Software Engineer Intern", "We care about security.") == ["general"]
    # a specific title wins over the description
    assert sectors_for("Frontend Developer Intern", desc) == ["frontend", "web"]


def test_labels():
    assert sector_label("embedded") == "🔌 Embarqué / Firmware"
    assert sector_label("embedded", emoji=False) == "Embarqué / Firmware"
    assert list(SECTORS)[-1] == "general"
