from generator.seo_scorer import SEOElementScorer



def test_keyword_score():

    score = SEOElementScorer.keyword_score(
        "washing machine start button replacement"
    )

    print(
        "keyword score:",
        score
    )

    assert score > 5



def test_model_score():

    score = SEOElementScorer.model_score(
        "WD-N10240D"
    )

    print(
        "model score:",
        score
    )

    assert score > 5
