# 最低方向数量修改证据

用户最新要求：10个候选中至少给出一个小长线置信度最高的方向，不能正常完成却全部无方向。

## 修改

- screening_v3：十份证据全覆盖；相对小长线方向排序，选1—3；选中项包含初判方向、排名、定性置信度与风险。
- direction_v11：普通首选的Schema仅允许LONG/SHORT，并由宿主再次校验；其他币保留SKIP。首选复核可依据完整一周证据更正初判方向。
- 不再执行旧筛选的绝对质量降级清空逻辑；保留真实引用校验、数据完整性和交易端执行门槛。允许LOW置信度，不伪造高置信或盈利概率。
- 首选没有发布明确方向则周期失败并持久告警；没有第二次模型调用或宿主伪造方向。
- 旧提示词和旧筛选/调度代码保存在20260915-before-minimum-direction目录；新提示词SHA256见minimum-direction-manifest.json。生产每次MODEL_INPUT、SCREENING_MODEL_INPUT另存实际提示词与SHA256。

参考 [OpenAI GPT-5.6 提示词指导](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.6#prompting-best-practices)：明确预期结果、减少重复指令，并用本业务样例验证修改。保持gpt-5.6-terra/medium，未更换模型。

新版真实调用结果以 ../ACCEPTANCE.md 与runtime中的模型事件为准，旧真实调用不作为新版验收。
