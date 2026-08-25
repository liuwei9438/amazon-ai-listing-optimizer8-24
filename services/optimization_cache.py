from __future__ import annotations

import hashlib
import json
from typing import Any


class OptimizationCache:


    @staticmethod
    def create_key(
        record
    ):

        data = {

            "sku":
                getattr(
                    record,
                    "sku",
                    ""
                ),

            "title":
                getattr(
                    record,
                    "title",
                    ""
                ),

            "bullets":
                getattr(
                    record,
                    "bullets",
                    []
                ),

            "description":
                getattr(
                    record,
                    "description",
                    ""

                )

        }


        text = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True
        )


        return hashlib.md5(
            text.encode("utf-8")
        ).hexdigest()



    @staticmethod
    def get(
        cache,
        key
    ):

        return cache.get(
            key
        )



    @staticmethod
    def set(
        cache,
        key,
        value
    ):

        cache[key]=value


        return cache
