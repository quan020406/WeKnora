# ParadeDB 存量库升级

仓库的生产与开发 Compose 使用 `paradedb/paradedb:v0.22.6-pg17`。本文处理 **0.22.2 → 0.22.6、PostgreSQL 主版本保持 17** 的升级；不适用于 PostgreSQL 跨主版本升级，也不代表 Helm 中其他旧版本可以直接套用。

## 升级步骤

所有命令在仓库根目录执行。开发环境的数据库命令使用 `docker compose -f docker-compose.dev.yml`，开发后端在宿主机上手动停止；开发 Compose 没有 `app` 服务。保留原数据卷，**不要执行 `down -v`、删除卷或换成 PG18 镜像**。

1. 停止 app 和其他数据库写入方；启用了 Langfuse 时也要停止其 web/worker，本地运行的开发后端同样需要停止。

   ```bash
   # 标准部署；开发环境请停止宿主机上的后端进程
   docker compose stop app
   # 仅在启用了 Langfuse 时执行
   docker compose stop langfuse-web langfuse-worker
   ```

2. 备份所有数据库和角色，包括 Langfuse 库，并确认能够恢复。备份存放在数据库数据卷之外。

   ```bash
   umask 077
   docker compose exec -T postgres sh -c 'pg_dumpall -U "$POSTGRES_USER"' > paradedb-before-upgrade.sql
   ```

   若旧镜像在当前 CPU 上无法启动，先保存停止状态的数据卷快照，在兼容机器上制作逻辑备份或验证快照可恢复，再改动唯一的数据副本。

3. 使用更新后的 Compose，只替换数据库容器：

   ```bash
   docker compose pull postgres
   docker compose up -d --no-deps --wait postgres
   ```

4. 完成扩展的 SQL 升级。仅替换镜像不会更新已有数据库内的 `pg_extension` 版本。

   ```bash
   docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
   ALTER EXTENSION pg_search UPDATE TO '0.22.6';
   SELECT extname, extversion FROM pg_extension WHERE extname IN ('pg_search', 'vector');
   SELECT * FROM paradedb.version_info();
   SQL
   ```

   在**其他已安装 `pg_search` 的数据库**中同样执行，包括安装过该扩展的 `postgres`、模板库或 Langfuse 库。不要为了升级而向原本不需要的数据库安装扩展。catalog 与 `paradedb.version_info()` 应均报告 0.22.6。

5. 恢复原先运行的写入服务，确认数据库迁移状态，执行有代表性的关键词与向量检索。这个补丁升级无需专门重建全部索引或重新导入文档；保留备份直到验证完成。

## 自动迁移的范围

迁移 `000099` 仅在 WeKnora 数据库内执行扩展升级：已安装版本须为 0.22.2–0.22.5，且服务器提供 0.22.6 扩展包。它遵守 `app.skip_embedding`，不安装缺失的扩展，也不处理其他版本线。

如果这条迁移在替换数据库镜像**之前**已经执行，安装新镜像后不会自动再跑一次，需要手动执行上面的 SQL。没有 `000099` 的旧应用也需手工升级。其他数据库的扩展不能靠 WeKnora 的迁移代管。

## 回滚与复现验证

回滚需要将升级前备份/快照恢复到独立数据卷，配合旧镜像使用。仅把镜像标签改回旧版不会撤销扩展 SQL 变更，`000099` 的 down 文件也不会尝试降级扩展。

仓库提供隔离验证脚本：

```bash
docker pull paradedb/paradedb:v0.22.2-pg17
docker pull paradedb/paradedb:v0.22.6-pg17
python3 scripts/test_paradedb_upgrade.py
```

脚本使用临时容器和卷，不映射端口；验证同一数据卷升级、表内容、关键词/向量检索、迁移幂等性和重启后结果，并输出备份与日志。该用例验证固定测试数据，部署时仍需检查自己的数据和检索负载。

## BM25 过滤字段索引迁移（000111）

`000111_bm25_filter_fields` 为关键词检索重建 `embeddings_search_idx`：

- 将 `knowledge_base_id` 配置为 `keyword` tokenizer 和 fast field，使知识库 ID 的等值/IN 过滤能够在 BM25 索引中执行。
- 将 `is_enabled` 加入索引并配置为 boolean fast field，保留 `true` 或 `NULL` 表示启用的查询语义。
- 保留 `content` 的 `chinese_lindera` tokenizer；不修改 embedding 数据、向量索引或检索 API。

关键词查询同时用 `paradedb.const_score(0, ...)` 表达这两个过滤条件。仅新增索引而保留原 SQL，会让过滤条件参与 BM25 评分；零分过滤保留内容相关性排序，并通过排除 `false` 保留历史 `NULL` 数据。旧堆过滤计划在组合文档过滤时可能重复计算内容分数，因此原始数值不保证逐项不变；这两个过滤条件自身不增加相关性分值；文档和标签过滤保留现有行为。回归测试用移除 KB/启用过滤后的查询核对分数，并对照旧查询验证结果顺序。应用查询与这条 migration 需要一起部署；回滚数据库时也应恢复对应的旧应用版本。

与上面的扩展补丁升级不同，这条迁移会重建 BM25 索引。重建期间会持有表锁，阻塞 `embeddings` 的读写；大库应安排维护窗口，预留索引重建时间与磁盘空间。迁移在单个原子 SQL 语句内替换索引；创建失败时旧索引会恢复，仍需按现有迁移流程处理失败状态后再启动应用。

已具有 `is_enabled` 列的旧初始化索引和版本化迁移创建的索引均可通过这条迁移升级。设置了 `app.skip_embedding=true`，或不存在 `embeddings` 表时会跳过。down 迁移恢复原来的字段和 tokenizer 配置，也需要重建索引并持锁；它不回滚扩展版本。

使用 Python 3 和 Docker 运行隔离回归验证：

```bash
python3 scripts/test_bm25_filter_fields.py
# 在迁移前索引上重现 heap_filter，预期以断言失败退出：
python3 scripts/test_bm25_filter_fields.py --baseline
```

脚本使用临时 ParadeDB 0.22.6 / PostgreSQL 17 容器，不映射端口，不挂载现有数据卷，并在结束时删除自己创建的容器和匿名卷。执行计划与数据库日志保存在输出目录；可通过 `--output-dir` 指定一个空目录。验证覆盖过滤下推、检索结果、启用状态更新、回滚/重应用、跳过 embedding、完整版本化新安装，以及旧 bootstrap 的 embeddings 表配合相关 embeddings 迁移。旧 bootstrap 全量接续 `000000` 存在既有的 `tenants.api_key` 字段不匹配，不属于本次索引变更的修复范围。固定测试数据的执行时间不能代表生产性能收益。
