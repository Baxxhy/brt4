请生成一个最小 surrogate source patch，用于判断当前 BRT 是否可能是 fail-to-pass 测试。

行为目标：
{behavior_json}

buggy 版本测试：
{final_test}

buggy 执行日志：
{buggy_execution_log}

允许修改的相关源码：
{code_context}

此前 surrogate 尝试及反馈：
{previous_attempts}

要求：
1. 只修改上面列出的生产源码文件，最多 3 个 search/replace；
2. 修补应实现 expected_behavior，而不是针对测试返回常量；
3. 不得修改任何 tests、test_*.py、conftest.py、配置或依赖文件；
4. search 必须逐字复制自提供源码，并且尽量是 3-30 行的小片段；不要复制或重写整个函数/类；
5. replace 只包含这个小片段的替换版本，必须和 search 有实质差异；
6. 如果前一次 patch 已经应用但测试仍失败，必须根据反馈改变根因假设或修改位置，不能原样重复 patch；
7. 如果证据不足，返回空 patches，不要编造。

只输出：
{{
  "analysis": "简短说明根因和修补策略",
  "confidence": "high|medium|low",
  "patches": [
    {{
      "path": "相关生产源码相对路径",
      "search": "需要替换的精确原始文本",
      "replace": "替换后的完整文本",
      "reason": "该修改如何实现 expected_behavior"
    }}
  ]
}}
