CREATE TABLE IF NOT EXISTS departments (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(150) NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS academic_terms (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    start_date DATE,
    end_date DATE,
    CHECK (start_date IS NULL OR end_date IS NULL OR end_date >= start_date)
);

CREATE TABLE IF NOT EXISTS cohorts (
    id BIGSERIAL PRIMARY KEY,
    department_id BIGINT NOT NULL REFERENCES departments(id),
    academic_term_id BIGINT NOT NULL REFERENCES academic_terms(id),
    level SMALLINT NOT NULL CHECK (level > 0),
    total_students INTEGER NOT NULL CHECK (total_students > 0),
    group_size INTEGER NOT NULL DEFAULT 20 CHECK (group_size BETWEEN 1 AND 20)
);

CREATE TABLE IF NOT EXISTS instructors (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    department_id BIGINT REFERENCES departments(id),
    can_teach_lecture BOOLEAN NOT NULL DEFAULT FALSE,
    can_teach_section BOOLEAN NOT NULL DEFAULT FALSE,
    can_teach_lab BOOLEAN NOT NULL DEFAULT FALSE,
    part_time BOOLEAN NOT NULL DEFAULT FALSE,
    friday_available BOOLEAN NOT NULL DEFAULT FALSE
);

ALTER TABLE instructors
    ADD COLUMN IF NOT EXISTS part_time BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS friday_available BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS can_teach_lecture BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS can_teach_section BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS can_teach_lab BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE instructors
    ALTER COLUMN can_teach_lecture SET DEFAULT FALSE,
    ALTER COLUMN can_teach_section SET DEFAULT FALSE,
    ALTER COLUMN can_teach_lab SET DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS courses (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(30) NOT NULL UNIQUE,
    name VARCHAR(150) NOT NULL,
    department_id BIGINT NOT NULL REFERENCES departments(id)
);

CREATE TABLE IF NOT EXISTS rooms (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    room_type VARCHAR(50) NOT NULL CHECK (room_type IN ('LECTURE', 'SECTION', 'LAB'))
);

CREATE TABLE IF NOT EXISTS time_slots (
    id BIGSERIAL PRIMARY KEY,
    day_of_week SMALLINT NOT NULL CHECK (day_of_week BETWEEN 1 AND 7),
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    CHECK (end_time > start_time),
    UNIQUE (day_of_week, start_time, end_time)
);

CREATE TABLE IF NOT EXISTS student_groups (
    id BIGSERIAL PRIMARY KEY,
    cohort_id BIGINT NOT NULL REFERENCES cohorts(id) ON DELETE CASCADE,
    group_number INTEGER NOT NULL CHECK (group_number > 0),
    student_count INTEGER NOT NULL CHECK (student_count > 0),
    UNIQUE (cohort_id, group_number)
);

CREATE TABLE IF NOT EXISTS course_offerings (
    id BIGSERIAL PRIMARY KEY,
    course_id BIGINT NOT NULL REFERENCES courses(id),
    cohort_id BIGINT NOT NULL REFERENCES cohorts(id) ON DELETE CASCADE,
    academic_term_id BIGINT NOT NULL REFERENCES academic_terms(id),
    instructor_id BIGINT NOT NULL REFERENCES instructors(id),
    weekly_lectures INTEGER NOT NULL DEFAULT 0 CHECK (weekly_lectures >= 0),
    weekly_sections INTEGER NOT NULL DEFAULT 0 CHECK (weekly_sections >= 0),
    weekly_labs INTEGER NOT NULL DEFAULT 0 CHECK (weekly_labs >= 0),
    has_lab BOOLEAN NOT NULL DEFAULT FALSE
);

ALTER TABLE course_offerings
    DROP CONSTRAINT IF EXISTS course_offerings_course_id_cohort_id_academic_term_id_key;

DROP INDEX IF EXISTS course_offerings_unique_lecture;
DROP INDEX IF EXISTS course_offerings_unique_section;
DROP INDEX IF EXISTS course_offerings_unique_lab;

ALTER TABLE course_offerings
    ADD COLUMN IF NOT EXISTS has_lab BOOLEAN;

UPDATE course_offerings
SET has_lab = (weekly_labs > 0)
WHERE has_lab IS NULL;

ALTER TABLE course_offerings
    ALTER COLUMN has_lab SET DEFAULT FALSE,
    ALTER COLUMN has_lab SET NOT NULL;

CREATE TABLE IF NOT EXISTS timetable_sessions (
    id BIGSERIAL PRIMARY KEY,
    offering_id BIGINT NOT NULL REFERENCES course_offerings(id) ON DELETE CASCADE,
    academic_term_id BIGINT NOT NULL REFERENCES academic_terms(id),
    instructor_id BIGINT NOT NULL REFERENCES instructors(id),
    room_id BIGINT NOT NULL REFERENCES rooms(id),
    time_slot_id BIGINT NOT NULL REFERENCES time_slots(id),
    activity_type VARCHAR(20) NOT NULL CHECK (activity_type IN ('lecture', 'section', 'lab')),
    session_number INTEGER NOT NULL CHECK (session_number > 0)
);

CREATE TABLE IF NOT EXISTS session_groups (
    session_id BIGINT NOT NULL REFERENCES timetable_sessions(id) ON DELETE CASCADE,
    time_slot_id BIGINT NOT NULL REFERENCES time_slots(id),
    group_id BIGINT NOT NULL REFERENCES student_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (session_id, time_slot_id, group_id)
);