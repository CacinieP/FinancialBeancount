# 本地账单工作台

本地浏览器应用，将「账单导入 → 分类复核 → 账簿核对 → Beancount 导出」放在同一个工作台。账单、模型和账簿保留在电脑上，不需要登录云服务。

默认模型选 MiniCPM-V 4.7 1B；保留换用更大模型的入口。1B 先承担未命中规则的普通收支建议，实际质量通过用户确认记录来衡量，不靠模型自报置信度代替评测。

## 图形工作台

当前新增功能从源码安装，尚未发布新版安装包。在仓库目录执行：

```sh
pip install -e '.[workbench,xlsx,pdf]'
beancount-workbench --model-file '/path/to/MiniCPM-V-4_7-1B-Q4_K_M.gguf' --open
```

工作台默认监听 `http://127.0.0.1:8765`。如果已经有本机 1B 服务，可省略 `--model-file`；没有模型时也能打开工作台，在「模型与规则」中选择 GGUF 文件。运行 GGUF 需要支持 MiniCPM-V 4.7 的本地 `llama-server`。已经运行的服务会被复用；工作台关闭时只停止自己启动的模型，不停止外部服务。

工作区默认保存在当前目录的 `workbench-data/`，可通过 `--workspace /path/to/workspace` 指定。导入的文件复制到这个目录，复核状态和规则会保存，重启可以恢复。页面提供明确标记的合成示例，供首次试用；用户主动载入示例时会替换当前工作区视图，原文件不会被修改。

从合成示例开始导入自己的文件时，示例交易会整体退出当前视图，避免混入真实账单。清空工作区只清空当前会话与复核视图，不删除原文件或本地上传副本。

四个页面分别提供：

- **交易复核**：选择或拖放 CSV、Excel、PDF，查看去重结果和分类依据，调用真实本机 1B，修改账户并逐笔或批量确认。
- **账簿核对**：导入已有 Beancount 文件或含 includes 的目录，选择主账簿，设置资产账户映射与日期窗口，查看已匹配、仅账单、仅账簿和歧义组。
- **导入导出**：预览并下载独立账单或 include 片段，可选择仅已确认交易、排除核对中已入账的交易；原生账簿导出保留原有结构。
- **模型与规则**：查看 1B 连接状态，选择本地 GGUF 文件，管理平台资产账户和商户分类规则。

「原生账簿」导出保留已导入账簿的指令与分录，**不会自动合并新账单**。新增交易使用交易片段导出，复核后纳入主账簿。未确认交易保留 `!`，未指定实际对方账户的转账不能直接确认。

图形工作台对自动匹配采用保守标准：账户、币种、金额和日期符合且交易对方一致，才计为已匹配；交易对方不同或缺失会进入待核对。导出默认保留全部交易，只有用户确认匹配无误并主动勾选后才排除已匹配记录。

上传账簿只能引用本次上传目录内的 include 和附件，不能通过绝对路径或 `..` 读取电脑其他文件；需要外部文件的账簿请一并整理到所选目录。用户插件不在工作台中执行。工作台只监听本机，写入操作校验页面会话，静态资源、字体和模型请求都不会使用远程服务。

## 当前可用的命令行功能

这些新增功能目前从当前源码安装，尚未发布新版安装包：

```sh
pip install -e '.[config,validate]'
```

在仓库目录运行。Excel/PDF 需要相应的 `xlsx` / `pdf` 额外依赖。

### 选择本地模型文件

安装支持 MiniCPM-V 4.7 的新版 [llama.cpp](https://github.com/ggml-org/llama.cpp)，使 `llama-server` 可在命令行找到。选择官方 GGUF 后运行：

```sh
beancount-dedup --model-file '/path/to/MiniCPM-V-4_7-1B-Q4_K_M.gguf'
```

程序会自动启动本机模型服务，处理 `input/` 内的账单，完成后关闭自己启动的服务。纯文本分类只需语言模型 GGUF；截图识别需要额外视觉投影和单独入口，当前尚未实现。模型文件就地读取，无需复制进项目。

也可以连接已经运行的本机 OpenAI-compatible 服务：配置 `ai.base_url` 和 `ai.model` 后，运行 `beancount-dedup --ai-classify`。只允许 loopback 地址，绕过代理，拒绝重定向；普通运行不会调用模型。

MiniCPM-V 4.7 提供 1B 和 35B-A3B 两种规格；具体部署支持见 [OpenBMB 官方指南](https://openbmb.cn/docs/en/guides/minicpm-v/minicpm-v-4.7/deploy)。模型不会随工具安装，也不会自动下载。

### 替换账户树

复制示例配置为 `beancount-dedup.yaml`，例如：

```yaml
accounts:
  rules_mode: replace
  platform_wallets:
    alipay: Assets:Alipay
  expense_rules:
    咖啡: Expenses:Coffee
  income_rules:
    工资: Income:Salary
ai:
  allowed_accounts:
    - Expenses:Coffee
    - Expenses:Groceries
    - Expenses:Transport
    - Income:Salary
```

`append` 保留内置规则，用户规则优先；`replace` 完全停用内置商户和分类规则。内置规则存放在包内 `default_account_rules.json`，无须安装 YAML 依赖即可使用默认值。

只有规则未命中的普通收支会请求模型；明确设置到 Unknown / Other 的用户规则也不会被覆盖。转账和退款不交给模型重新分类。请求只含交易对方、描述、收支方向和可选账户列表，不发送原始账单、交易 ID、金额或支付方式。

模型给出的账户必须在允许列表内并满足方向约束。达到配置阈值的建议会进入新导出文件，但始终标记 `!`；低置信、无效输出、服务故障和达到请求上限时保留原分类并标记复核。详情见 `output/classification_report.json`。置信度是模型的自评分，不能视为实测准确率。

### 核对已有账簿

```sh
beancount-dedup --ledger '/path/to/main.beancount'
# 可与 --model-file 或 --ai-classify 一起使用
```

`output/reconciliation_report.json` 按资产账户、币种、带正负号的精确金额和日期逐笔比对，列出已匹配、仅账单存在、仅账簿存在和歧义组。默认同日匹配；跨日可用 `--date-window-days 1`。同额多候选不会随意配对，账簿不会被修改。

核对范围按输入涉及的各账户日期范围推断，不代表完整期间覆盖或期初余额验证。输入账单没有币种字段，统一使用配置的 `operating_currency`，请勿将混合币种账单放在同一轮处理中。需要将配置的资产账户名与现有账簿对齐。

带用户插件的账簿会明确拒绝，应先在自己的 Beancount 环境导出不含插件指令的普通账簿。核对过程中不运行账簿插件、不读取或写入其 pickle 缓存。

### 原生 Beancount 导入、导出

```sh
beancount-dedup --beancount '/path/to/main.beancount' -o 'output/imported.beancount'
```

原生账簿使用 Beancount 的指令与 posting 结构，不经过银行账单的简化交易模型。导入、导出的文件会做语法和记账有效性校验，导出目标不得覆盖原文件及其 include 文件。注释、空白和原来的文件拆分不作为格式保留目标；支持边界与无法展平的选项会明确报错。

### 接入已有 Beancount / Fava 工作流

主账簿仍是数据源，账户树直接使用已有命名。把新交易导成片段，再在主账簿中 include：

```sh
beancount-dedup --fragment -o output/import.beancount
bean-check /path/to/main.beancount
fava /path/to/main.beancount
```

在主账簿中增加 `include "相对路径/output/import.beancount"`，并在主账簿的账户文件中声明需要的账户。先复核 `!` 交易与去重报告，再决定纳入哪些交易。`--ledger` 只生成差异报告，不会自动从新导出中删除已经入账的交易。

也可以使用标准 [beangulp](https://github.com/beancount/beangulp) 导入器，让现有导入流程或 [Fava 导入页](https://github.com/beancount/fava/blob/main/src/fava/help/import.md) 调用本工具的 CSV 解析与分类：

```sh
pip install -e '.[beangulp,config]'
```

在自己的导入配置文件中注册：

```python
from beancount_dedup.beangulp_importer import FinancialBeancountImporter
from beancount_dedup.config import load_config

CONFIG = [FinancialBeancountImporter(load_config("/path/to/beancount-dedup.yaml"))]

if __name__ == "__main__":
    import beangulp
    beangulp.Ingest(CONFIG)()
```

供 beangulp 命令行使用时，按官方配置方式调用 `beangulp.Ingest(CONFIG)`；Fava 配置中使用同一份 `CONFIG`，设置 `import-config` 和 `import-dirs`。该适配器返回原生 Transaction 指令，不自动声明账户、不调用模型。已有账簿的重复标记要求本工具的稳定 ID 和完整交易内容一致；没有对应 ID 的旧账保留供核对，不用近似金额自动认定重复。支持已识别的支付宝、微信、银行、云闪付 CSV；Excel/PDF 先转换后再导入。

新导入流程会过滤明确取消的订单；有效交易若日期、金额损坏或没有明确收支方向，会拒绝导入该文件并报告问题，避免静默漏行或猜测资产增减。明确方向的内部转账暂挂 `Equity:Transfers:Unassigned` 并标记 `!`，复核时需替换成真实对方资产账户。模型不会处理这些资金方向问题。

桌面产品应沿用同样的原生指令、导入器和校验接口，让用户随时回到自己的 Beancount/Fava 工具链；无需迁移到专有数据库。

## 后续增强

- 推荐模型自动下载、校验、取消与断点续传。
- 从人工修改中提议更精确的商户规则，由用户确认后启用。
- 截图与扫描 PDF 的视觉入口，以及更细的账簿合并预览。

当前以本地单用户工作台为范围，优先 CSV/Excel 和原生 Beancount。多设备同步和模型微调留待真实使用数据验证后再增加。
