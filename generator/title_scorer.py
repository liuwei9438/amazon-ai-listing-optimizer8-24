from __future__ import annotations


class TitleScorer:


    @staticmethod
    def keyword_score(title, keywords):

        score = 0

        text = title.lower()


        for keyword in keywords:

            if keyword.lower() in text:

                score += 10


        return score



    @staticmethod
    def model_score(title, models):

        score = 0

        text = title.lower()


        for model in models:

            if model.lower() in text:

                score += 8


        return score



    @staticmethod
    def length_score(title):

        length = len(title)


        if length > 75:

            return -50


        # 亚马逊标题尽量利用字符空间

        if length >= 60:

            return 10


        elif length >= 45:

            return 7


        else:

            return 3



    @staticmethod
    def total_score(
        title,
        keywords,
        models
    ):


        return (

            TitleScorer.keyword_score(
                title,
                keywords
            )
            +
            TitleScorer.model_score(
                title,
                models
            )
            +
            TitleScorer.length_score(
                title
            )

        )
