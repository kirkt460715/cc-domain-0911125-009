# 试验数据版本与批准发布

内容寻址的数据对象与目录版本独立保存。发布计划需要用途和批准；补丁、模式迁移及保留期审查各自留痕。

运行需要 Python 3.11 或更高版本，使用标准库与 SQLite，无需第三方包。演练中的机构、人员、交易及读数全部为本地合成数据。

## 构建与测试

```sh
python3 -m compileall -q trialvault
python3 -m unittest discover -s tests -v
```

## 本地运行

```sh
python3 -m trialvault demo
python3 -m trialvault --help
```

演练使用临时数据库，不会连接远程服务。命令以 JSON 显示业务结果；测试覆盖核心状态、边界输入和错误路径。运行前可用 `python3 --version` 核对解释器版本。
