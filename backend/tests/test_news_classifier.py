import pytest

from aegis.news import classifier


def test_source_quality_score_matches_spec_values_exactly():
    assert classifier.source_quality_score("PRIMARY_OFFICIAL") == pytest.approx(1.00)
    assert classifier.source_quality_score("TIER_1_FINANCIAL_MEDIA") == pytest.approx(0.90)
    assert classifier.source_quality_score("SPECIALIZED_CRYPTO_MEDIA") == pytest.approx(0.75)
    assert classifier.source_quality_score("SOCIAL_MEDIA") == pytest.approx(0.30)
    assert classifier.source_quality_score("UNVERIFIED") == pytest.approx(0.00)


def test_source_quality_score_unknown_tier_falls_back_to_unverified():
    assert classifier.source_quality_score("SOMETHING_MADE_UP") == pytest.approx(0.00)


def test_extract_assets_finds_whole_word_mentions_only():
    assets = classifier.extract_assets("Bitcoin surged today while Ethereum lagged")
    assert set(assets) == {"BTC", "ETH"}


def test_extract_assets_does_not_match_inside_other_words():
    # "ETH" must not match inside "method", "SOL" must not match inside "resolution"
    assets = classifier.extract_assets("This method requires a resolution before launch")
    assert "ETH" not in assets
    assert "SOL" not in assets


def test_extract_assets_matches_ticker_form_too():
    assets = classifier.extract_assets("XRP and BNB both moved higher")
    assert set(assets) == {"XRP", "BNB"}


def test_extract_assets_empty_when_nothing_mentioned():
    assert classifier.extract_assets("The weather was nice today") == []


def test_classify_sentiment_positive_when_positive_keywords_dominate():
    result = classifier.classify_sentiment("Regulator grants approval for new partnership and listing")
    assert result.sentiment == "positive"
    assert result.score > 0
    assert result.confidence > 0


def test_classify_sentiment_negative_when_negative_keywords_dominate():
    result = classifier.classify_sentiment("Exchange hacked, lawsuit filed after fraud investigation")
    assert result.sentiment == "negative"
    assert result.score < 0


def test_classify_sentiment_neutral_with_no_lexicon_hits():
    result = classifier.classify_sentiment("Quarterly report released on schedule")
    assert result.sentiment == "neutral"
    assert result.score == 0.0
    assert result.confidence == 0.0


def test_classify_sentiment_neutral_when_evenly_balanced():
    result = classifier.classify_sentiment("Approval granted, but a lawsuit was also filed")
    assert result.sentiment == "neutral"
    assert result.score == pytest.approx(0.0)


def test_classify_sentiment_confidence_scales_with_hit_count():
    weak = classifier.classify_sentiment("A partnership was announced")
    strong = classifier.classify_sentiment("Approval, partnership, launch and listing all announced")
    assert strong.confidence > weak.confidence


def test_classify_magnitude_high_for_regulatory_keywords():
    assert classifier.classify_magnitude("SEC files lawsuit over ETF approval") == "HIGH"


def test_classify_magnitude_medium_for_generic_sentiment_keywords():
    assert classifier.classify_magnitude("Company announces a new partnership") == "MEDIUM"


def test_classify_magnitude_low_with_no_signal_words():
    assert classifier.classify_magnitude("Quarterly report released on schedule") == "LOW"
