"""Local OpenAI-compatible API pool.

Each list index describes one independent API account. This module must never
print or serialize keys into experiment outputs.
"""

API_NAMES: list[str] = [
    "fa_254701003",
    "fa_244711003",
    "fa_254711063",
    "fa_244701007",
    "fa_254711072",
    "tao_yifan",
    "fa_254711067",
    "fa_251812017",
    "extra_20260628",
    "fa_254711066",
    "fa_254711092",
]

API_KEYS: list[str] = [
    "sk-lZfASZqz0EyU13GNFhT8uVfUQD3aC6umuIFozrg6HTz1VDgq",
    "sk-23Nv7ebOWzNr4I05UX9YmWzEwp7JCSx5kfjGbKIVJ0ChCmWK",
    "sk-8nOXxOG58owlpoGCHxPmou1IFdvqmllHW4CKXURWlPMTX1lN",
    "sk-C5qhFRG0JcccispW1tZ5EGFp5vdpYJod1Yaj7eW5hGCNFeeV",
    "sk-BPWARlpCwipqJWGLbgA8thLTs5C0ukyRkx0G7D4gHajNxrFb",
    "sk-1LXIfDaj5uXPDGxaoOnPF3yVeCx3ihmAunaiMx8y2egJSfZt",
    "sk-fsgAIDD91mdxR8FOyf5a7yfACLxMekCP1ubfBhfadBBh0Tw7",
    "sk-2I1EnoJE82BLWl5EgVR7Gds0Em3suNPaML2gTdxnJG7coy9o",
    "sk-h0cFjLrI80RePeqK7hyFUzW4IrVo9u1GJUX9rTmZVe85xqBP",
    "sk-pmywSXZJOlYHkEG5QfqjuH8INPelZ4oZ2Y7H9zcGFbeiWUWq",
    "sk-VR3OX99efraKcDvjCP3bAo41PXVRG3VNfTZaN4NEFtSZz2N9",
]

API_BASE_URLS: list[str] = [
    "https://api.chat.csu.edu.cn/v1",
    "https://api.chat.csu.edu.cn/v1",
    "https://api.chat.csu.edu.cn/v1",
    "https://api.chat.csu.edu.cn/v1",
    "https://api.chat.csu.edu.cn/v1",
    "https://api.chat.csu.edu.cn/v1",
    "https://api.chat.csu.edu.cn/v1",
    "https://api.chat.csu.edu.cn/v1",
    "https://api.chat.csu.edu.cn/v1",
    "https://api.chat.csu.edu.cn/v1",
    "https://api.chat.csu.edu.cn/v1",
]

API_MODELS: list[str] = [
    "deepseek-v3",
    "deepseek-v3",
    "deepseek-v3",
    "deepseek-v3",
    "deepseek-v3",
    "deepseek-v3",
    "deepseek-v3",
    "deepseek-v3",
    "deepseek-v3",
    "deepseek-v3",
    "deepseek-v3",
]


def configured_apis() -> list[tuple[str, str, str]]:
    """Return validated (key, base_url, model) entries without logging secrets."""
    keys = [value.strip() for value in API_KEYS if value.strip()]
    if not keys:
        return []
    if len(API_NAMES) != len(keys):
        raise ValueError("API_NAMES must match API_KEYS length")
    if len(API_BASE_URLS) not in {1, len(keys)}:
        raise ValueError("API_BASE_URLS must contain one value or match API_KEYS length")
    if len(API_MODELS) not in {0, 1, len(keys)}:
        raise ValueError("API_MODELS must be empty, contain one value, or match API_KEYS length")
    bases = API_BASE_URLS * len(keys) if len(API_BASE_URLS) == 1 else API_BASE_URLS
    if not API_MODELS:
        models = [""] * len(keys)
    else:
        models = API_MODELS * len(keys) if len(API_MODELS) == 1 else API_MODELS
    return [
        (key, bases[index].strip(), models[index].strip())
        for index, key in enumerate(keys)
    ]
