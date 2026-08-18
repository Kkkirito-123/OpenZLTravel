CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION catalogowner;

CREATE TABLE IF NOT EXISTS app.schemaversion (
    version INTEGER PRIMARY KEY,
    appliedat TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE app.schemaversion IS '业务数据库结构版本';
COMMENT ON COLUMN app.schemaversion.version IS '已应用的结构版本号';
COMMENT ON COLUMN app.schemaversion.appliedat IS '版本应用时间';

CREATE TABLE IF NOT EXISTS app.visitor (
    visitorid UUID PRIMARY KEY,
    tokenhash CHAR(64) NOT NULL UNIQUE,
    createdat TIMESTAMPTZ NOT NULL,
    lastseenat TIMESTAMPTZ NOT NULL,
    expiresat TIMESTAMPTZ NOT NULL
);
COMMENT ON TABLE app.visitor IS '匿名浏览器访客身份';
COMMENT ON COLUMN app.visitor.visitorid IS '匿名访客稳定编号';
COMMENT ON COLUMN app.visitor.tokenhash IS '浏览器随机 Token 的 SHA-256 哈希';
COMMENT ON COLUMN app.visitor.createdat IS '身份创建时间';
COMMENT ON COLUMN app.visitor.lastseenat IS '最近访问时间';
COMMENT ON COLUMN app.visitor.expiresat IS '匿名身份过期时间';

CREATE TABLE IF NOT EXISTS app.trip (
    tripid UUID PRIMARY KEY,
    visitorid UUID NOT NULL REFERENCES app.visitor(visitorid),
    destination TEXT NOT NULL,
    startdate DATE NOT NULL,
    enddate DATE NOT NULL,
    summary TEXT NOT NULL,
    createdat TIMESTAMPTZ NOT NULL,
    requestjson JSONB NOT NULL,
    itineraryjson JSONB NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);
COMMENT ON TABLE app.trip IS '最终行程和生成请求快照';
COMMENT ON COLUMN app.trip.tripid IS '行程唯一编号';
COMMENT ON COLUMN app.trip.visitorid IS '行程所属匿名访客';
COMMENT ON COLUMN app.trip.destination IS '目的地展示名称';
COMMENT ON COLUMN app.trip.startdate IS '旅行开始日期';
COMMENT ON COLUMN app.trip.enddate IS '旅行结束日期';
COMMENT ON COLUMN app.trip.summary IS '行程摘要';
COMMENT ON COLUMN app.trip.createdat IS '行程创建时间';
COMMENT ON COLUMN app.trip.requestjson IS '生成请求 JSON 快照';
COMMENT ON COLUMN app.trip.itineraryjson IS '完整行程 JSON 快照';
COMMENT ON COLUMN app.trip.revision IS '并发编辑版本号';

CREATE TABLE IF NOT EXISTS app.planningsession (
    sessionid UUID PRIMARY KEY,
    visitorid UUID NOT NULL REFERENCES app.visitor(visitorid),
    idempotencykey TEXT,
    status TEXT NOT NULL,
    requestjson JSONB NOT NULL,
    sessionjson JSONB NOT NULL,
    createdat TIMESTAMPTZ NOT NULL,
    updatedat TIMESTAMPTZ NOT NULL,
    UNIQUE (visitorid, idempotencykey)
);
COMMENT ON TABLE app.planningsession IS '可恢复的旅行规划任务状态';
COMMENT ON COLUMN app.planningsession.sessionid IS '规划会话唯一编号';
COMMENT ON COLUMN app.planningsession.visitorid IS '规划会话所属匿名访客';
COMMENT ON COLUMN app.planningsession.idempotencykey IS '访客范围内的请求幂等键';
COMMENT ON COLUMN app.planningsession.status IS '规划会话当前状态';
COMMENT ON COLUMN app.planningsession.requestjson IS '规划请求 JSON 快照';
COMMENT ON COLUMN app.planningsession.sessionjson IS '完整规划会话 JSON 快照';
COMMENT ON COLUMN app.planningsession.createdat IS '会话创建时间';
COMMENT ON COLUMN app.planningsession.updatedat IS '会话最近更新时间';

CREATE TABLE IF NOT EXISTS app.dialoguesession (
    sessionid UUID PRIMARY KEY,
    visitorid UUID NOT NULL REFERENCES app.visitor(visitorid),
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    activeflow TEXT,
    statejson JSONB NOT NULL,
    planningsessionid UUID,
    createdat TIMESTAMPTZ NOT NULL,
    updatedat TIMESTAMPTZ NOT NULL,
    UNIQUE (visitorid, sessionid)
);
COMMENT ON TABLE app.dialoguesession IS '旅行助手权威对话状态';
COMMENT ON COLUMN app.dialoguesession.sessionid IS '助手会话唯一编号';
COMMENT ON COLUMN app.dialoguesession.visitorid IS '助手会话所属匿名访客';
COMMENT ON COLUMN app.dialoguesession.revision IS '并发消息乐观版本号';
COMMENT ON COLUMN app.dialoguesession.status IS '助手会话当前状态';
COMMENT ON COLUMN app.dialoguesession.activeflow IS '当前旅行对话流程';
COMMENT ON COLUMN app.dialoguesession.statejson IS '权威旅行槽位 JSON';
COMMENT ON COLUMN app.dialoguesession.planningsessionid IS '已启动的规划会话编号';
COMMENT ON COLUMN app.dialoguesession.createdat IS '会话创建时间';
COMMENT ON COLUMN app.dialoguesession.updatedat IS '会话最近更新时间';

CREATE TABLE IF NOT EXISTS app.dialoguerequest (
    sessionid UUID NOT NULL,
    visitorid UUID NOT NULL,
    messageid UUID NOT NULL,
    requestcontent TEXT NOT NULL,
    responsejson JSONB NOT NULL,
    createdat TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (sessionid, messageid),
    FOREIGN KEY (visitorid, sessionid)
        REFERENCES app.dialoguesession(visitorid, sessionid)
        ON DELETE CASCADE ON UPDATE CASCADE
);
COMMENT ON TABLE app.dialoguerequest IS '助手消息幂等请求与响应';
COMMENT ON COLUMN app.dialoguerequest.sessionid IS '所属助手会话编号';
COMMENT ON COLUMN app.dialoguerequest.visitorid IS '消息所属匿名访客';
COMMENT ON COLUMN app.dialoguerequest.messageid IS '客户端消息幂等编号';
COMMENT ON COLUMN app.dialoguerequest.requestcontent IS '原始用户消息';
COMMENT ON COLUMN app.dialoguerequest.responsejson IS '已返回的助手响应 JSON';
COMMENT ON COLUMN app.dialoguerequest.createdat IS '响应提交时间';

ALTER TABLE app.dialoguerequest
    DROP CONSTRAINT IF EXISTS dialoguerequest_visitorid_sessionid_fkey;
ALTER TABLE app.dialoguerequest
    ADD CONSTRAINT dialoguerequest_visitorid_sessionid_fkey
    FOREIGN KEY (visitorid, sessionid)
    REFERENCES app.dialoguesession(visitorid, sessionid)
    ON DELETE CASCADE ON UPDATE CASCADE;

CREATE TABLE IF NOT EXISTS app.travelmemory (
    visitorid UUID NOT NULL REFERENCES app.visitor(visitorid),
    memorykey TEXT NOT NULL,
    valuejson JSONB NOT NULL,
    version INTEGER NOT NULL,
    sourcesessionid UUID NOT NULL,
    createdat TIMESTAMPTZ NOT NULL,
    updatedat TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (visitorid, memorykey)
);
COMMENT ON TABLE app.travelmemory IS '访客明确保存的长期旅行偏好';
COMMENT ON COLUMN app.travelmemory.visitorid IS '偏好所属匿名访客';
COMMENT ON COLUMN app.travelmemory.memorykey IS '稳定偏好字段名';
COMMENT ON COLUMN app.travelmemory.valuejson IS '偏好值 JSON';
COMMENT ON COLUMN app.travelmemory.version IS '偏好版本号';
COMMENT ON COLUMN app.travelmemory.sourcesessionid IS '最近一次明确修改来源会话';
COMMENT ON COLUMN app.travelmemory.createdat IS '偏好首次创建时间';
COMMENT ON COLUMN app.travelmemory.updatedat IS '偏好最近更新时间';

CREATE TABLE IF NOT EXISTS app.legacyclaim (
    claimid UUID PRIMARY KEY,
    visitorid UUID NOT NULL REFERENCES app.visitor(visitorid),
    tokenhash CHAR(64) NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'claimed')),
    createdat TIMESTAMPTZ NOT NULL,
    expiresat TIMESTAMPTZ NOT NULL,
    claimedby UUID REFERENCES app.visitor(visitorid),
    claimedat TIMESTAMPTZ
);
COMMENT ON TABLE app.legacyclaim IS '旧 SQLite 数据的一次性认领凭据';
COMMENT ON COLUMN app.legacyclaim.claimid IS '认领记录唯一编号';
COMMENT ON COLUMN app.legacyclaim.visitorid IS '旧数据临时归属访客';
COMMENT ON COLUMN app.legacyclaim.tokenhash IS '一次性认领码 SHA-256 哈希';
COMMENT ON COLUMN app.legacyclaim.status IS '待认领或已认领';
COMMENT ON COLUMN app.legacyclaim.createdat IS '认领码创建时间';
COMMENT ON COLUMN app.legacyclaim.expiresat IS '认领码过期时间';
COMMENT ON COLUMN app.legacyclaim.claimedby IS '成功认领的当前访客';
COMMENT ON COLUMN app.legacyclaim.claimedat IS '成功认领时间';

CREATE TABLE IF NOT EXISTS app.importrecord (
    importid UUID PRIMARY KEY,
    sourcehash CHAR(64) NOT NULL UNIQUE,
    sourcefile TEXT NOT NULL,
    countsjson JSONB NOT NULL,
    importedat TIMESTAMPTZ NOT NULL
);
COMMENT ON TABLE app.importrecord IS '一次性 SQLite 导入审计记录';
COMMENT ON COLUMN app.importrecord.importid IS '导入记录唯一编号';
COMMENT ON COLUMN app.importrecord.sourcehash IS '源 SQLite 文件 SHA-256';
COMMENT ON COLUMN app.importrecord.sourcefile IS '不含目录的源文件名';
COMMENT ON COLUMN app.importrecord.countsjson IS '各表导入数量';
COMMENT ON COLUMN app.importrecord.importedat IS '导入完成时间';

CREATE TABLE IF NOT EXISTS app.session_turns (
    session_id TEXT NOT NULL,
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    user_content TEXT NOT NULL,
    assistant_content TEXT NOT NULL,
    archived BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (session_id, sequence)
);
COMMENT ON TABLE app.session_turns IS 'OpenZLAgent 固定会话轮次表';
COMMENT ON COLUMN app.session_turns.session_id IS 'OpenZLAgent 会话编号';
COMMENT ON COLUMN app.session_turns.sequence IS '会话内连续轮次序号';
COMMENT ON COLUMN app.session_turns.user_content IS '用户消息正文';
COMMENT ON COLUMN app.session_turns.assistant_content IS '助手消息正文';
COMMENT ON COLUMN app.session_turns.archived IS '是否已进入滚动摘要归档';
COMMENT ON COLUMN app.session_turns.created_at IS '轮次创建时间';
COMMENT ON COLUMN app.session_turns.metadata_json IS '上下文引用和模型用量元数据';

CREATE TABLE IF NOT EXISTS app.session_summaries (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    summarized_through_sequence BIGINT NOT NULL,
    summary TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_turn_count INTEGER NOT NULL,
    estimated_input_tokens INTEGER NOT NULL,
    estimated_output_tokens INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (session_id, summarized_through_sequence)
);
COMMENT ON TABLE app.session_summaries IS 'OpenZLAgent 固定会话摘要表';
COMMENT ON COLUMN app.session_summaries.id IS '摘要唯一编号';
COMMENT ON COLUMN app.session_summaries.session_id IS 'OpenZLAgent 会话编号';
COMMENT ON COLUMN app.session_summaries.summarized_through_sequence IS '摘要覆盖到的连续轮次';
COMMENT ON COLUMN app.session_summaries.summary IS '滚动摘要正文';
COMMENT ON COLUMN app.session_summaries.reason IS '触发摘要的稳定原因';
COMMENT ON COLUMN app.session_summaries.source_turn_count IS '摘要包含的原始轮次数量';
COMMENT ON COLUMN app.session_summaries.estimated_input_tokens IS '摘要模型估算输入 Token';
COMMENT ON COLUMN app.session_summaries.estimated_output_tokens IS '摘要模型估算输出 Token';
COMMENT ON COLUMN app.session_summaries.created_at IS '摘要创建时间';
COMMENT ON COLUMN app.session_summaries.metadata_json IS '摘要过程的安全元数据';

CREATE INDEX IF NOT EXISTS tripvisitoridx ON app.trip(visitorid, createdat DESC);
CREATE INDEX IF NOT EXISTS planningsessionvisitoridx
    ON app.planningsession(visitorid, updatedat DESC);
CREATE INDEX IF NOT EXISTS planningsessionstatusidx ON app.planningsession(status, createdat);
CREATE INDEX IF NOT EXISTS dialoguesessionvisitoridx
    ON app.dialoguesession(visitorid, updatedat DESC);
CREATE INDEX IF NOT EXISTS session_turns_hot_idx
    ON app.session_turns(session_id, archived, sequence DESC);

INSERT INTO app.schemaversion(version) VALUES (1) ON CONFLICT (version) DO NOTHING;
DROP TABLE IF EXISTS app.providercache;
INSERT INTO app.schemaversion(version) VALUES (2) ON CONFLICT (version) DO NOTHING;

GRANT USAGE ON SCHEMA app TO travelapp;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO travelapp;
ALTER DEFAULT PRIVILEGES FOR ROLE catalogowner IN SCHEMA app
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO travelapp;
