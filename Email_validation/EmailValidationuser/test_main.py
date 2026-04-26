"""
Tests for merged Panelist Duplicate Check API
Run: pytest test_main.py -v
"""

import pytest
from main import (
    _decompose,
    _email_similarity,
    _fingerprint_score,
    _verdict,
    _match_tier,
    check_panelist_similarity,
    PANELIST_CACHE,
    DuplicateCheckRequest,
)


# ═══════════════════════════════════════════════════════
# EMAIL DECOMPOSITION  (your original normalize cases)
# ═══════════════════════════════════════════════════════

class TestDecompose:
    def test_title_prefix_stripped(self):
        d = _decompose("mr.john@gmail.com")
        assert "mr" not in d["alpha"]

    def test_dr_prefix_stripped(self):
        d = _decompose("dr.smith@gmail.com")
        assert d["alpha"] == "smith"

    def test_plus_alias_stripped(self):
        assert _decompose("john+promo@gmail.com")["local"] == "john"

    def test_gmail_dot_trick(self):
        assert _decompose("j.o.h.n@gmail.com")["local"] == "john"

    def test_alpha_digit_split(self):
        d = _decompose("davidmiller739100@gmail.com")
        assert d["alpha"] == "davidmiller"
        assert d["digits"] == "739100"

    def test_domain_alias(self):
        assert _decompose("x@googlemail.com")["domain"] == "gmail.com"

    def test_invalid_email_returns_empty(self):
        assert _decompose("notanemail") == {}


# ═══════════════════════════════════════════════════════
# EMAIL SIMILARITY
# ═══════════════════════════════════════════════════════

class TestEmailSimilarity:
    def test_exact_canonical_match(self):
        score, reasons = _email_similarity("John@Gmail.com", "john@gmail.com")
        assert score == 100.0
        assert "exact_email_match" in reasons

    def test_cross_domain_fraud(self):
        score, reasons = _email_similarity(
            "davidmiller739100@gmail.com",
            "davidmiller739100@outlook.com",
        )
        assert score == 97.0
        assert "cross_domain_username_match" in reasons

    def test_case1_numeric_padding(self):
        """davidmiller739100 vs davidmiller79210 — original Case 1"""
        score, _ = _email_similarity(
            "davidmiller739100@gmail.com",
            "davidmiller79210@gmail.com",
        )
        assert score >= 80

    def test_case2_previously_missed(self):
        """davidmiller739100 vs davidmiller4207776 — was missed at raw 86"""
        score, _ = _email_similarity(
            "davidmiller739100@gmail.com",
            "davidmiller4207776@gmail.com",
        )
        assert score >= 70   # now caught by decomposed scorer

    def test_case3_flagged(self):
        score, _ = _email_similarity(
            "davidmiller40976@gmail.com",
            "davidmiller4207776@gmail.com",
        )
        assert score >= 75

    def test_completely_different(self):
        score, _ = _email_similarity("alice@gmail.com", "zxqrt123@yahoo.com")
        assert score < 40

    def test_match_tier_labels(self):
        assert _match_tier(95) == "HIGH"
        assert _match_tier(75) == "MEDIUM"
        assert _match_tier(50) == "LOW"


# ═══════════════════════════════════════════════════════
# FINGERPRINT SCORER
# ═══════════════════════════════════════════════════════

NEW_P = {
    "device_id":  "dev-abc",
    "ip_address": "192.168.1.10",
    "os":         "windows 11",
    "latitude":   28.6139,
    "longitude":  77.2090,
    "country":    "india",
}

SAME_P = {
    "device_id":  "dev-abc",
    "ip_address": "192.168.1.10",
    "os":         "windows 11",
    "latitude":   28.6139,
    "longitude":  77.2090,
    "country":    "india",
}

class TestFingerprintScorer:
    def test_all_match_100(self):
        score, bd, _ = _fingerprint_score(NEW_P, SAME_P)
        assert score == 100
        assert all(bd.values())

    def test_zero_match(self):
        other = {
            "device_id": "dev-xyz", "ip_address": "10.0.0.1",
            "os": "macos", "latitude": 51.5, "longitude": -0.1,
            "country": "uk",
        }
        score, bd, _ = _fingerprint_score(NEW_P, other)
        assert score == 0

    def test_20_per_param(self):
        """Each param is exactly 20 pts"""
        for param, val in [
            ("device_id", "dev-abc"),
            ("os",        "windows 11"),
            ("country",   "india"),
        ]:
            only = {k: None for k in NEW_P}
            only[param] = val
            existing = {k: None for k in SAME_P}
            existing[param] = val
            score, _, _ = _fingerprint_score(only, existing)
            assert score == 20, f"{param} should give 20 pts"

    def test_subnet_ip_match_gives_20(self):
        new_p    = {**NEW_P, "ip_address": "192.168.1.55"}  # same /24
        score, bd, reasons = _fingerprint_score(new_p, SAME_P)
        assert bd["ip"] is True
        assert "subnet_ip_match" in reasons

    def test_exact_location_match(self):
        score, bd, reasons = _fingerprint_score(NEW_P, SAME_P)
        assert bd["location"] is True
        assert "exact_location_match" in reasons

    def test_different_location_no_match(self):
        new_p = {**NEW_P, "latitude": 19.0760, "longitude": 72.8777}  # Mumbai
        _, bd, _ = _fingerprint_score(new_p, SAME_P)
        assert bd["location"] is False

    def test_missing_fields_safe(self):
        score, _, _ = _fingerprint_score({}, {})
        assert score == 0


# ═══════════════════════════════════════════════════════
# VERDICT ENGINE
# ═══════════════════════════════════════════════════════

class TestVerdict:
    def test_exact_email_always_rejected(self):
        assert _verdict(100.0, 0) == "REJECTED"

    def test_high_sim_plus_2fp_rejected(self):
        assert _verdict(92.0, 40) == "REJECTED"

    def test_strong_device_match_rejected(self):
        """4–5 fingerprint params = same machine → REJECTED regardless of email"""
        assert _verdict(50.0, 80) == "REJECTED"

    def test_email_only_is_review_not_rejected(self):
        """john80 vs john81 — high email sim but no device match → REVIEW"""
        assert _verdict(88.0, 0) == "REVIEW"

    def test_single_fp_param_is_review(self):
        assert _verdict(50.0, 20) == "REVIEW"

    def test_below_threshold_accepted(self):
        assert _verdict(40.0, 0) == "ACCEPTED"


# ═══════════════════════════════════════════════════════
# FULL INTEGRATION (mocked cache)
# ═══════════════════════════════════════════════════════

MOCK_CACHE = [
    {
        "panelistId":       4401,
        "email":            "davidmiller79210@gmail.com",
        "normalized_email": "davidmiller79210@gmail.com",
        "alpha_username":   "davidmiller",
        "ip_address":       "192.168.1.10",
        "device_id":        "dev-abc",
        "os":               "windows 11",
        "latitude":         28.6139,
        "longitude":        77.2090,
        "country":          "india",
    },
    {
        "panelistId":       9999,
        "email":            "alice@gmail.com",
        "normalized_email": "alice@gmail.com",
        "alpha_username":   "alice",
        "ip_address":       "10.0.0.1",
        "device_id":        "dev-xyz",
        "os":               "macos",
        "latitude":         51.5,
        "longitude":        -0.1,
        "country":          "uk",
    },
]


@pytest.fixture(autouse=True)
def inject_mock_cache(monkeypatch):
    import main
    monkeypatch.setattr(main, "PANELIST_CACHE", MOCK_CACHE)


class TestFullIntegration:
    def _req(self, email="davidmiller739100@gmail.com", pid=9021):
        return DuplicateCheckRequest(
            panelistId=pid,
            email=email,
            ip_address="192.168.1.10",
            device_id="dev-abc",
            os="windows 11",
            latitude=28.6139,
            longitude=77.2090,
            country="india",
        )

    def test_response_has_all_keys(self):
        result = check_panelist_similarity(self._req())
        for key in ("input_email", "panelistId", "match_count",
                    "matches", "similarity_score", "fingerprint_score", "verdict"):
            assert key in result

    def test_correct_match_found(self):
        result = check_panelist_similarity(self._req())
        pids = [m["panelistId"] for m in result["matches"]]
        assert 4401 in pids
        assert 9999 not in pids   # alice is unrelated

    def test_self_excluded(self):
        req = self._req(email="davidmiller79210@gmail.com", pid=4401)
        result = check_panelist_similarity(req)
        assert 4401 not in [m["panelistId"] for m in result["matches"]]

    def test_verdict_is_rejected(self):
        result = check_panelist_similarity(self._req())
        assert result["verdict"] == "REJECTED"

    def test_fingerprint_score_is_highest(self):
        """Top-level fingerprint_score = max across all matches"""
        result = check_panelist_similarity(self._req())
        max_fp = max(m["fingerprint_score"] for m in result["matches"])
        assert result["fingerprint_score"] == max_fp

    def test_similarity_score_is_highest(self):
        result = check_panelist_similarity(self._req())
        max_sim = max(m["similarity_score"] for m in result["matches"])
        assert result["similarity_score"] == max_sim

    def test_empty_cache_raises(self, monkeypatch):
        import main
        monkeypatch.setattr(main, "PANELIST_CACHE", [])
        with pytest.raises(Exception, match="cache not loaded"):
            check_panelist_similarity(self._req())
