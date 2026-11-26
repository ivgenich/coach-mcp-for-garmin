-- Session keys as dates (plan_store.OLD_KEY_RE): plans stored before the key change carry week-numbered keys
-- ("w03-long", "w00-e1", "w05-race", "w04-b"; a retired row "w03-long~<hash8>[-<n>]") in their version bodies,
-- workout rows, feedback, changes and tokens. Each becomes the date key of its session or row
-- (plan_engine.session_key: YYYYMMDD-run, -race, -b), once. MemoryPlanStore.upgrade_keys() is the same
-- conversion in Python; test_plan_store runs one contract on both. Nothing is dropped; a second run changes
-- nothing (no old key is left to match).
--   1. rows, per plan, linked first, then those that may be on Garmin (plan_store.LIVE_STATES), then the newest:
--      the date key of date_local (a retired row keeps its suffix); a key the plan already holds becomes the
--      retired key "<date key>~<md5(old key)[:8]>[-<n>]" (the next commit deletes that duplicate from Garmin).
--      The feedback of an old session key moves to the row's date key (dropped when that key has feedback).
--   2. versions: each old session key in the body, and the same JSON string in its inputs, becomes the date key
--      of the session's date; for the plan's current version also the feedback left and the plan's changes.
--   3. feedback still under an old key is deleted; open tokens whose ops name an old key expire.

DO $$
DECLARE
    r     record;
    v     record;
    m     jsonb;
    k     text;
    nk    text;
    base  text;
    cand  text;
    h     text;
    n     integer;
    b     text;
    i     text;
    live  text[] := ARRAY['uploading', 'uploaded', 'scheduling', 'scheduled', 'unscheduling', 'unknown'];
BEGIN
    FOR r IN
        SELECT plan_id, workout_key, date_local FROM history.plan_workouts
        WHERE workout_key ~ '^w[0-9]{2}-[a-z0-9]{1,8}(~[0-9a-f]{8}(-[1-9][0-9]{0,2})?)?$'
        ORDER BY plan_id COLLATE "C", (linked_activity_id IS NOT NULL) DESC, (state = ANY(live)) DESC,
                 state_changed_at DESC, workout_key COLLATE "C"
    LOOP
        base := to_char(r.date_local, 'YYYYMMDD') || '-' ||
                CASE substring(r.workout_key FROM '^w[0-9]{2}-([a-z0-9]{1,8})')
                    WHEN 'race' THEN 'race' WHEN 'b' THEN 'b' ELSE 'run' END;
        cand := base || coalesce(substring(r.workout_key FROM '(~[0-9a-f]{8}(-[1-9][0-9]{0,2})?)$'), '');
        IF EXISTS (SELECT 1 FROM history.plan_workouts WHERE plan_id = r.plan_id AND workout_key = cand) THEN
            h := substr(md5(r.workout_key), 1, 8);
            cand := base || '~' || h;
            n := 1;
            WHILE EXISTS (SELECT 1 FROM history.plan_workouts WHERE plan_id = r.plan_id AND workout_key = cand) LOOP
                n := n + 1;
                cand := base || '~' || h || '-' || n;
            END LOOP;
        END IF;
        UPDATE history.plan_workouts SET workout_key = cand
            WHERE plan_id = r.plan_id AND workout_key = r.workout_key;
        IF position('~' IN r.workout_key) = 0 THEN
            IF EXISTS (SELECT 1 FROM history.plan_feedback WHERE plan_id = r.plan_id AND workout_key = base) THEN
                DELETE FROM history.plan_feedback WHERE plan_id = r.plan_id AND workout_key = r.workout_key;
            ELSE
                UPDATE history.plan_feedback SET workout_key = base
                    WHERE plan_id = r.plan_id AND workout_key = r.workout_key;
            END IF;
        END IF;
    END LOOP;

    FOR v IN
        SELECT pv.plan_id, pv.version, pv.body, pv.inputs, (pv.version = p.current_version) AS current
        FROM history.plan_versions pv JOIN history.plans p ON p.plan_id = pv.plan_id
        WHERE pv.body::text ~ '"w[0-9]{2}-[a-z0-9]{1,8}"'
        ORDER BY pv.plan_id COLLATE "C", pv.version
    LOOP
        SELECT jsonb_object_agg(s->>'key', replace(s->>'date', '-', '') || '-' ||
                   CASE substring(s->>'key' FROM '^w[0-9]{2}-([a-z0-9]{1,8})$')
                       WHEN 'race' THEN 'race' WHEN 'b' THEN 'b' ELSE 'run' END)
            INTO m
            FROM jsonb_array_elements(CASE WHEN jsonb_typeof(v.body->'weeks') = 'array' THEN v.body->'weeks'
                                           ELSE '[]'::jsonb END) AS w,
                 jsonb_array_elements(CASE WHEN jsonb_typeof(w->'sessions') = 'array' THEN w->'sessions'
                                           ELSE '[]'::jsonb END) AS s
            WHERE jsonb_typeof(s) = 'object' AND jsonb_typeof(s->'key') = 'string'
              AND jsonb_typeof(s->'date') = 'string'
              AND s->>'key' ~ '^w[0-9]{2}-[a-z0-9]{1,8}$' AND s->>'date' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$';
        IF m IS NULL THEN
            CONTINUE;
        END IF;
        b := v.body::text;
        i := v.inputs::text;
        FOR k, nk IN SELECT key, value FROM jsonb_each_text(m) LOOP
            b := replace(b, '"' || k || '"', '"' || nk || '"');
            i := replace(i, '"' || k || '"', '"' || nk || '"');
            IF v.current THEN
                IF EXISTS (SELECT 1 FROM history.plan_feedback WHERE plan_id = v.plan_id AND workout_key = nk) THEN
                    DELETE FROM history.plan_feedback WHERE plan_id = v.plan_id AND workout_key = k;
                ELSE
                    UPDATE history.plan_feedback SET workout_key = nk WHERE plan_id = v.plan_id AND workout_key = k;
                END IF;
                UPDATE history.plan_changes
                    SET from_value = replace(from_value::text, '"' || k || '"', '"' || nk || '"')::jsonb,
                        to_value = replace(to_value::text, '"' || k || '"', '"' || nk || '"')::jsonb,
                        evidence = replace(evidence::text, '"' || k || '"', '"' || nk || '"')::jsonb
                    WHERE plan_id = v.plan_id;
            END IF;
        END LOOP;
        UPDATE history.plan_versions SET body = b::jsonb, inputs = i::jsonb
            WHERE plan_id = v.plan_id AND version = v.version;
    END LOOP;

    DELETE FROM history.plan_feedback
        WHERE workout_key ~ '^w[0-9]{2}-[a-z0-9]{1,8}(~[0-9a-f]{8}(-[1-9][0-9]{0,2})?)?$';
    UPDATE history.plan_tokens SET status = 'expired'
        WHERE status IN ('issued', 'running', 'partial') AND ops::text ~ '"w[0-9]{2}-[a-z0-9]{1,8}[~"]';
END
$$;
