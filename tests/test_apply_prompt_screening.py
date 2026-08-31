"""Regression tests for apply/prompt.py's screening-question and hard-rules text.

Two real bugs this guards against, both found by watching the actual
auto-apply agent answer screening questions wrong on a live form:

1. Relocation was a hardcoded "cannot relocate" string in the prompt,
   completely disconnected from any profile field or the location the user
   typed at `applypilot init`.
2. Work authorization read correctly as True/False in the profile, but the
   hard-rules text replaced the explicit yes/no with just a permit-type
   label (e.g. "opt EAD") whenever one was set -- which the agent then had
   to infer authorization status from, and it inferred wrong.
"""

from applypilot.apply.prompt import _build_hard_rules, _build_screening_section

BASE_PROFILE = {
    "personal": {
        "full_name": "Test Candidate",
        "preferred_name": "",
        "city": "Phoenix",
    },
    "experience": {
        "years_of_experience_total": "3",
        "target_role": "AI Engineer",
    },
}


def _profile_with_work_auth(**overrides) -> dict:
    profile = {**BASE_PROFILE, "work_authorization": {
        "legally_authorized_to_work": True,
        "require_sponsorship": False,
        "work_permit_type": "",
        "willing_to_relocate": False,
    }}
    profile["work_authorization"].update(overrides)
    return profile


def test_screening_section_reflects_willing_to_relocate_true():
    profile = _profile_with_work_auth(willing_to_relocate=True)

    section = _build_screening_section(profile)

    assert "OPEN to relocating" in section
    assert "cannot relocate" not in section


def test_screening_section_reflects_willing_to_relocate_false():
    profile = _profile_with_work_auth(willing_to_relocate=False)

    section = _build_screening_section(profile)

    assert "cannot relocate" in section
    assert "OPEN to relocating" not in section


def test_screening_section_states_authorization_explicitly():
    profile = _profile_with_work_auth(legally_authorized_to_work=True)

    section = _build_screening_section(profile)

    assert "YES, authorized" in section


def test_hard_rules_states_yes_even_with_a_permit_type_set():
    """The exact bug: a permit type (e.g. OPT EAD) must not replace the
    explicit YES/NO -- both must be present so the agent isn't left to
    infer authorization status from a permit label alone.
    """
    profile = _profile_with_work_auth(
        legally_authorized_to_work=True,
        work_permit_type="opt EAD",
    )

    rules = _build_hard_rules(profile)

    assert "authorized to work = YES" in rules
    assert "opt EAD" in rules


def test_hard_rules_states_no_when_not_authorized():
    profile = _profile_with_work_auth(legally_authorized_to_work=False)

    rules = _build_hard_rules(profile)

    assert "authorized to work = NO" in rules
