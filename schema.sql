-- ============================================================
-- LIVEKIT OUTBOUND CALL SYSTEM — Supabase Schema
-- Run this in your Supabase SQL editor
-- ============================================================

-- Main calls table
CREATE TABLE IF NOT EXISTS calls (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id       TEXT        UNIQUE NOT NULL,           -- internal ID we generate
    to_number     TEXT        NOT NULL,
    from_number   TEXT,
    status        TEXT        NOT NULL DEFAULT 'initiated',
    -- statuses: initiated | dialing | ringing | answered | completed | failed | rejected | no_answer | voicemail
    payload       JSONB       NOT NULL DEFAULT '{}',     -- variables sent at call init
    instructions  TEXT,                                  -- resolved system prompt
    livekit_room  TEXT,                                  -- LiveKit room name
    egress_id     TEXT,                                  -- LiveKit Egress ID for recording
    recording_url TEXT,                                  -- Supabase storage URL after call ends
    duration_secs INTEGER,                               -- call duration in seconds
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    answered_at   TIMESTAMPTZ,
    ended_at      TIMESTAMPTZ
);

-- Per-utterance transcripts (both user and agent speech)
CREATE TABLE IF NOT EXISTS transcripts (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id    TEXT        NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    role       TEXT        NOT NULL CHECK (role IN ('user', 'agent')),
    text       TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Every LLM tool call during the call
CREATE TABLE IF NOT EXISTS tool_calls (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id     TEXT        NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    tool_name   TEXT        NOT NULL,
    args        JSONB       NOT NULL DEFAULT '{}',
    result      JSONB,
    status      TEXT        NOT NULL DEFAULT 'pending', -- pending | success | error
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

-- Granular call lifecycle events
CREATE TABLE IF NOT EXISTS call_events (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id    TEXT        NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    event_type TEXT        NOT NULL,
    -- event types: dialing | ringing | answered | agent_speaking | user_speaking |
    --              tool_called | forwarding | forwarded | ended | recording_ready | error
    data       JSONB       NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for fast queries
CREATE INDEX IF NOT EXISTS idx_calls_status      ON calls(status);
CREATE INDEX IF NOT EXISTS idx_calls_created     ON calls(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_transcripts_call  ON transcripts(call_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tool_calls_call   ON tool_calls(call_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_call       ON call_events(call_id, created_at);

-- Enable Row Level Security (optional but recommended)
ALTER TABLE calls       ENABLE ROW LEVEL SECURITY;
ALTER TABLE transcripts ENABLE ROW LEVEL SECURITY;
ALTER TABLE tool_calls  ENABLE ROW LEVEL SECURITY;
ALTER TABLE call_events ENABLE ROW LEVEL SECURITY;

-- Service role bypass (for your backend using service_role key)
CREATE POLICY "service_role_all" ON calls       FOR ALL USING (TRUE);
CREATE POLICY "service_role_all" ON transcripts FOR ALL USING (TRUE);
CREATE POLICY "service_role_all" ON tool_calls  FOR ALL USING (TRUE);
CREATE POLICY "service_role_all" ON call_events FOR ALL USING (TRUE);
