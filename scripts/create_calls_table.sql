-- Create agent_calls table for call logging
-- Run this in your Supabase SQL editor

CREATE TABLE IF NOT EXISTS agent_calls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES agent_users(id) ON DELETE SET NULL,
    call_id TEXT UNIQUE NOT NULL,  -- Vapi call ID
    phone_number TEXT NOT NULL,
    call_type TEXT NOT NULL DEFAULT 'general',  -- restaurant_reservation, general, etc.
    status TEXT NOT NULL DEFAULT 'initiated',   -- initiated, ringing, in-progress, ended
    ended_reason TEXT,
    duration_seconds INTEGER,
    transcript TEXT,
    summary TEXT,
    success BOOLEAN,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    ended_at TIMESTAMPTZ
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_agent_calls_user_id ON agent_calls(user_id);
CREATE INDEX IF NOT EXISTS idx_agent_calls_call_id ON agent_calls(call_id);
CREATE INDEX IF NOT EXISTS idx_agent_calls_status ON agent_calls(status);
CREATE INDEX IF NOT EXISTS idx_agent_calls_created_at ON agent_calls(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_calls_call_type ON agent_calls(call_type);

-- RLS policies
ALTER TABLE agent_calls ENABLE ROW LEVEL SECURITY;

-- Users can view their own calls
CREATE POLICY "Users can view own calls"
    ON agent_calls FOR SELECT
    USING (
        user_id IN (
            SELECT id FROM agent_users WHERE telegram_id = current_setting('app.telegram_id', true)
        )
    );

-- Service role can do everything
CREATE POLICY "Service role full access"
    ON agent_calls FOR ALL
    USING (auth.role() = 'service_role');

-- Comment
COMMENT ON TABLE agent_calls IS 'Logs all outbound voice calls made by Connect Smart';
