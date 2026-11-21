-- SPDX-License-Identifier: AGPL-3.0-only
-- the rule against double booking by spot, installed only in
-- a disposable proof database. Not a product migration. No user-defined
-- function, trigger or extension.
CREATE TABLE organization (id bigint PRIMARY KEY CHECK (id > 0));
CREATE TABLE space (
    id bigint PRIMARY KEY CHECK (id > 0),
    organization_id bigint NOT NULL REFERENCES organization,
    UNIQUE (organization_id, id)
);

-- The space key is the asset's space, or the asset alone without one. The
-- mark says the asset covers its whole space.
CREATE TABLE asset (
    id bigint PRIMARY KEY CHECK (id > 0),
    organization_id bigint NOT NULL REFERENCES organization,
    space_id bigint,
    whole_space boolean NOT NULL DEFAULT false,
    scope_key bigint GENERATED ALWAYS AS (coalesce(space_id, -id)) STORED,
    CHECK (NOT whole_space OR space_id IS NOT NULL),
    FOREIGN KEY (organization_id, space_id) REFERENCES space (organization_id, id),
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, id, scope_key, whole_space)
);
-- One asset per space carries the mark (asset_whole_space_once): with two,
-- no native key would keep their shared claims apart.
CREATE UNIQUE INDEX asset_whole_space_once ON asset (space_id) WHERE whole_space;

CREATE TABLE floor_plan (
    id bigint PRIMARY KEY CHECK (id > 0),
    organization_id bigint NOT NULL,
    asset_id bigint NOT NULL,
    FOREIGN KEY (organization_id, asset_id) REFERENCES asset (organization_id, id),
    UNIQUE (organization_id, asset_id, id),
    UNIQUE (organization_id, id)
);

-- A claim holds a spot by its id, never its number.
CREATE TABLE spot (
    id bigint PRIMARY KEY CHECK (id > 0),
    organization_id bigint NOT NULL,
    floor_plan_id bigint NOT NULL,
    number integer NOT NULL CHECK (number > 0),
    FOREIGN KEY (organization_id, floor_plan_id)
        REFERENCES floor_plan (organization_id, id),
    UNIQUE (floor_plan_id, number),
    UNIQUE (organization_id, floor_plan_id, id)
);

-- One taken time per booking or block. The space key and the mark are
-- copies of the asset's, and follow it when it moves.
CREATE TABLE taken_time (
    id bigint PRIMARY KEY,
    organization_id bigint NOT NULL,
    asset_id bigint NOT NULL,
    scope_key bigint NOT NULL,
    whole_space boolean NOT NULL,
    mode text NOT NULL CHECK (mode IN ('shared', 'exclusive')),
    quantity integer NOT NULL CHECK (quantity > 0),
    state text NOT NULL CHECK (state IN
        ('held', 'confirmed', 'blocked', 'event', 'cancelled', 'expired')),
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    required_claims integer GENERATED ALWAYS AS
        (CASE WHEN mode = 'exclusive' THEN 1 ELSE quantity END) STORED,
    CHECK (isfinite(starts_at) AND isfinite(ends_at) AND starts_at < ends_at),
    CHECK (mode <> 'exclusive' OR quantity = 1),
    FOREIGN KEY (organization_id, asset_id, scope_key, whole_space)
        REFERENCES asset (organization_id, id, scope_key, whole_space)
        ON UPDATE CASCADE,
    UNIQUE (id, organization_id, asset_id, scope_key, whole_space, mode,
            quantity, state, starts_at, ends_at)
);

-- A shared claim of quantity q holds q spots, ordinals 1..q, each for the
-- whole time; an exclusive claim one row with no spot, the whole asset.
CREATE TABLE allocation (
    taken_time_id bigint NOT NULL,
    ordinal integer NOT NULL,
    organization_id bigint NOT NULL,
    asset_id bigint NOT NULL,
    floor_plan_id bigint,
    spot_id bigint,
    scope_key bigint NOT NULL,
    whole_space boolean NOT NULL,
    mode text NOT NULL,
    quantity integer NOT NULL,
    state text NOT NULL,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    previous_ordinal integer GENERATED ALWAYS AS
        (greatest(1, ordinal - 1)) STORED,
    PRIMARY KEY (taken_time_id, ordinal),
    CHECK (ordinal >= 1
           AND ordinal <= CASE WHEN mode = 'exclusive' THEN 1 ELSE quantity END),
    CHECK ((mode = 'exclusive' AND spot_id IS NULL AND floor_plan_id IS NULL)
           OR (mode = 'shared' AND spot_id IS NOT NULL
               AND floor_plan_id IS NOT NULL)),
    FOREIGN KEY (taken_time_id, organization_id, asset_id, scope_key,
                 whole_space, mode, quantity, state, starts_at, ends_at)
        REFERENCES taken_time (id, organization_id, asset_id, scope_key,
            whole_space, mode, quantity, state, starts_at, ends_at)
        ON UPDATE CASCADE,
    -- With the header's reference to the last one, no ordinal is missing.
    FOREIGN KEY (taken_time_id, previous_ordinal)
        REFERENCES allocation (taken_time_id, ordinal)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (organization_id, floor_plan_id, spot_id)
        REFERENCES spot (organization_id, floor_plan_id, id),
    FOREIGN KEY (organization_id, asset_id, floor_plan_id)
        REFERENCES floor_plan (organization_id, asset_id, id),
    -- A range that is everything overlaps every other: the asset for a
    -- marked asset, the spot on a marked asset for the unmarked ones and for
    -- an exclusive claim, and the spot on an unmarked asset for the marked
    -- ones and for an exclusive claim.
    CONSTRAINT capacity_no_double_booking EXCLUDE USING gist (
        int8range(organization_id, organization_id, '[]') WITH &&,
        int8range(scope_key, scope_key, '[]') WITH &&,
        (CASE WHEN whole_space THEN int8range(NULL, NULL)
              ELSE int8range(asset_id, asset_id, '[]') END) WITH &&,
        (CASE WHEN mode = 'exclusive' OR NOT whole_space THEN int8range(NULL, NULL)
              ELSE int8range(spot_id, spot_id, '[]') END) WITH &&,
        (CASE WHEN mode = 'exclusive' OR whole_space THEN int8range(NULL, NULL)
              ELSE int8range(spot_id, spot_id, '[]') END) WITH &&,
        tstzrange(starts_at, ends_at, '[)') WITH &&
    ) WHERE (state IN ('held', 'confirmed', 'blocked', 'event'))
      DEFERRABLE INITIALLY IMMEDIATE
);
ALTER TABLE taken_time ADD CONSTRAINT complete_allocation
    FOREIGN KEY (id, required_claims)
    REFERENCES allocation (taken_time_id, ordinal)
    DEFERRABLE INITIALLY DEFERRED;
