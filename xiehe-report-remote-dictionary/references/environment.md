# 远程字典版运行环境

本版通过系统SSH隧道和`psycopg`读取PostgreSQL。依赖存在、网络可达、当前字典可读、契约兼容、站月有数、Word验收是不同检查；不能互相代替。

## Python和本机依赖

使用宿主Python，依赖见[requirements.txt](../assets/project/requirements.txt)，使用所选Python的`-m pip install -r ...`安装。Codex可先调用load_workspace_dependencies。不要修改系统Python。

远程字典读取需要系统`ssh`和`psycopg`。若Word运行时和数据库Python不同，用`XIEHE_DICTIONARY_PYTHON`指定已安装`psycopg`的Python绝对路径。运行`PYTHON SKILL/scripts/doctor.py`检查本机路径与模块；实际`dictionary pull`才证明当前连接可用。

## 连接配置

[服务器连接.json](../assets/project/数据字典/服务器连接.json)仅包含非密连接默认值：SSH `10.10.245.103:22` / `dict_tunnel_huangbo`，跳板后的PostgreSQL `127.0.0.1:55432` / 数据库`report_platform` / 只读用户`dict_reader_huangbo`。这里的127.0.0.1指跳板可达端点，不是使用者电脑已有数据库。

个人连接信息放Skill外JSON，并将文件权限设为0600，再用`XIEHE_DICTIONARY_CONFIG`指向该文件。支持的键包括：

| 键 | 用途 |
|---|---|
| ssh_host、ssh_port、ssh_user | SSH跳板地址、端口与用户 |
| remote_host、remote_port | 从跳板访问的数据库地址与端口 |
| database、user | PostgreSQL数据库与只读用户 |
| ssh_password、password | SSH密码可选（也可用密钥）；当前读取器要求数据库密码。只写外部配置或环境变量 |
| identity_file、known_hosts | SSH私钥与主机公钥记录的文件路径 |

也可逐项设置`XIEHE_DICTIONARY_SSH_HOST`、`SSH_PORT`、`SSH_USER`、`REMOTE_HOST`、`REMOTE_PORT`、`DATABASE`、`USER`、`SSH_PASSWORD`、`PASSWORD`、`IDENTITY_FILE`、`KNOWN_HOSTS`，每个短名都需带`XIEHE_DICTIONARY_`前缀。配置路径示例：

```text
chmod 600 /绝对路径/个人配置/协合字典连接.json
export XIEHE_DICTIONARY_CONFIG=/绝对路径/个人配置/协合字典连接.json
PYTHON SKILL/scripts/run.py dictionary pull --out 新字典证据目录
```

勿将密码直接放在命令行参数，不输出连接文件全文。凭据、私钥、known_hosts和个人连接JSON留在Skill外，不进入Git或安装包。只读字典视图及当前兼容边界见[服务器字典说明](../assets/project/数据字典/README.md)。本Skill不代替服务端维护方发布新字典。

## 报告阶段的环境

兼容检查通过后，真实站月取数仍需要使用者自己的Power+ CLI及登录。已有适配器使用`~/Library/Application Support/xhyw-power-cli`下的CLI模块及自带Python，也调用PATH中的power命令；包内没有CLI安装包、密码或会话。实际查询才证明目标电站权限和当期数据可用。

Word预览需要宿主documents渲染器、LibreOffice/Poppler及中文字体；Codex可加载documents技能准备渲染。找不到渲染器时不能把未渲染Word称作交付通过。当前验证目标为Mac上的Codex；其他Agent或操作系统未经验证，不能宣称即装即用。

本版不再导出本地公共字典Excel，字典读取不需要Node或`@oai/artifact-tool`。完整安装本Skill目录，并保留[报告目录](../assets/project/报告模板/README.md)、远程连接代码及根电站配置；仅复制Word或SKILL.md不能运行。所有输出和当次字典快照置于Skill外，快照不是下一次运行的数据源。
