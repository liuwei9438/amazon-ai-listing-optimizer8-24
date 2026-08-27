from __future__ import annotations


class ModelRanker:
    """
    Simple model ranking engine

    V2.2.3

    Current strategy:
    - Keep original model order
    - Remove duplicates
    - Limit model numbers
    - No complex calculation

    Future:
    - Search volume
    - Amazon keyword popularity
    - Conversion data
    """


    @staticmethod
    def rank(models):

        if not models:
            return []


        result = []

        seen = set()


        for model in models:

            if not model:
                continue


            model = model.strip()


            if model.lower() in seen:
                continue


            seen.add(
                model.lower()
            )


            result.append(model)


        return result



    @staticmethod
    def select_top_models(
        models,
        limit=3
    ):
        """
        Select models for Amazon title

        Default:
        keep first 3 models

        """

        ranked = ModelRanker.rank(models)


        return ranked[:limit]
