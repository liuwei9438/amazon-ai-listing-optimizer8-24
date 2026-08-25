from __future__ import annotations


class KeywordRanker:


    @staticmethod
    def rank(keywords):

        scored = []


        for keyword in keywords:

            score = 0

            keyword = keyword.strip()


            text = keyword.lower()


            # 产品核心词
            if "replacement" in text:
                score += 3


            if "button" in text:
                score += 3


            if "part" in text:
                score += 1


            if "repair" in text:
                score -= 1


            # 长度控制
            if 15 <= len(keyword) <= 40:
                score += 2


            scored.append(
                {
                    "keyword": keyword,
                    "score": score
                }
            )


        scored.sort(
            key=lambda x:x["score"],
            reverse=True
        )


        return [
            x["keyword"]
            for x in scored
        ]
