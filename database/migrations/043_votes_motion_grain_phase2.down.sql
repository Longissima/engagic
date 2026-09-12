-- Fails while any matter carries more than one motion per member per meeting.
-- That is intended: restoring the constraint would otherwise mean deciding
-- which recorded votes to destroy.
ALTER TABLE votes ADD CONSTRAINT votes_council_member_id_matter_id_meeting_id_key
    UNIQUE (council_member_id, matter_id, meeting_id);
