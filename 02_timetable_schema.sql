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
    code VARCHAR(30) NOT NULL,
    name VARCHAR(150) NOT NULL,
    department_id BIGINT NOT NULL REFERENCES departments(id)
);

ALTER TABLE courses
    DROP CONSTRAINT IF EXISTS courses_code_key;

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

CREATE TABLE IF NOT EXISTS course_offerings (
    id BIGSERIAL PRIMARY KEY,
    course_id BIGINT NOT NULL REFERENCES courses(id),
    academic_term_id BIGINT NOT NULL REFERENCES academic_terms(id),
    instructor_id BIGINT NOT NULL REFERENCES instructors(id),
    total_students INTEGER NOT NULL CHECK (total_students > 0),
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

ALTER TABLE course_offerings
    ADD COLUMN IF NOT EXISTS total_students INTEGER;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'courses' AND column_name = 'total_students'
    ) THEN
        UPDATE course_offerings co
        SET total_students = c.total_students
        FROM courses c
        WHERE co.course_id = c.id AND co.total_students IS NULL;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'cohorts' AND column_name = 'total_students'
    ) THEN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'course_offerings' AND column_name = 'cohort_id'
        ) THEN
            UPDATE course_offerings co
            SET total_students = c.total_students
            FROM cohorts c
            WHERE co.cohort_id = c.id AND co.total_students IS NULL;
        END IF;
    END IF;
END;
$$;

UPDATE course_offerings
SET total_students = 1
WHERE total_students IS NULL;

ALTER TABLE course_offerings
    ALTER COLUMN total_students SET NOT NULL;

ALTER TABLE course_offerings
    DROP CONSTRAINT IF EXISTS course_offerings_total_students_check;

ALTER TABLE course_offerings
    ADD CONSTRAINT course_offerings_total_students_check
        CHECK (total_students > 0);

ALTER TABLE courses
    DROP CONSTRAINT IF EXISTS courses_total_students_check;

ALTER TABLE courses
    DROP COLUMN IF EXISTS total_students;

CREATE TABLE IF NOT EXISTS student_groups (
    id BIGSERIAL PRIMARY KEY,
    course_id BIGINT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    academic_term_id BIGINT NOT NULL REFERENCES academic_terms(id) ON DELETE CASCADE,
    group_number INTEGER NOT NULL CHECK (group_number > 0),
    student_count INTEGER NOT NULL CHECK (student_count > 0)
);

ALTER TABLE student_groups
    ADD COLUMN IF NOT EXISTS course_id BIGINT
        REFERENCES courses(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS academic_term_id BIGINT
        REFERENCES academic_terms(id) ON DELETE CASCADE;

ALTER TABLE student_groups
    ALTER COLUMN course_id DROP NOT NULL,
    ALTER COLUMN academic_term_id DROP NOT NULL;

ALTER TABLE student_groups
    DROP CONSTRAINT IF EXISTS student_groups_offering_id_group_number_key,
    DROP CONSTRAINT IF EXISTS student_groups_cohort_id_group_number_key;

DROP INDEX IF EXISTS student_groups_offering_id_group_number_key;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'student_groups' AND column_name = 'offering_id'
    ) THEN
        DELETE FROM timetable_sessions;

        UPDATE student_groups sg
        SET course_id = co.course_id,
            academic_term_id = co.academic_term_id
        FROM course_offerings co
        WHERE sg.offering_id = co.id;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'student_groups' AND column_name = 'cohort_id'
    ) AND EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'course_offerings' AND column_name = 'cohort_id'
    ) THEN
        DELETE FROM timetable_sessions;

        ALTER TABLE student_groups
            ALTER COLUMN cohort_id DROP NOT NULL;

        INSERT INTO student_groups
            (course_id, academic_term_id, group_number, student_count)
        SELECT co.course_id, co.academic_term_id,
               legacy.group_number, legacy.student_count
        FROM student_groups legacy
        JOIN course_offerings co ON co.cohort_id = legacy.cohort_id;
    END IF;
END;
$$;

DELETE FROM student_groups
WHERE course_id IS NULL OR academic_term_id IS NULL;

WITH duplicate_groups AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY course_id, academic_term_id, group_number
               ORDER BY id
           ) AS row_number
    FROM student_groups
)
DELETE FROM student_groups groups_to_delete
USING duplicate_groups
WHERE groups_to_delete.id = duplicate_groups.id
  AND duplicate_groups.row_number > 1;

ALTER TABLE student_groups
    DROP COLUMN IF EXISTS offering_id CASCADE,
    DROP COLUMN IF EXISTS cohort_id CASCADE;

ALTER TABLE course_offerings
    DROP COLUMN IF EXISTS cohort_id CASCADE;

DROP TABLE IF EXISTS cohorts CASCADE;

ALTER TABLE student_groups
    ALTER COLUMN course_id SET NOT NULL,
    ALTER COLUMN academic_term_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS student_groups_course_term_group_number_key
    ON student_groups (course_id, academic_term_id, group_number);

CREATE TABLE IF NOT EXISTS timetable_sessions (
    id BIGSERIAL PRIMARY KEY,
    offering_id BIGINT NOT NULL REFERENCES course_offerings(id) ON DELETE CASCADE,
    academic_term_id BIGINT NOT NULL REFERENCES academic_terms(id),
    instructor_id BIGINT NOT NULL REFERENCES instructors(id),
    room_id BIGINT NOT NULL REFERENCES rooms(id),
    time_slot_id BIGINT NOT NULL REFERENCES time_slots(id),
    activity_type VARCHAR(20) NOT NULL CHECK (activity_type IN ('LECTURE', 'SECTION', 'LAB')),
    session_number INTEGER NOT NULL CHECK (session_number > 0)
);

ALTER TABLE timetable_sessions
    DROP CONSTRAINT IF EXISTS timetable_sessions_activity_type_check;

ALTER TABLE timetable_sessions
    ADD CONSTRAINT timetable_sessions_activity_type_check
        CHECK (activity_type IN ('LECTURE', 'SECTION', 'LAB'));

CREATE TABLE IF NOT EXISTS session_groups (
    session_id BIGINT NOT NULL REFERENCES timetable_sessions(id) ON DELETE CASCADE,
    time_slot_id BIGINT NOT NULL REFERENCES time_slots(id),
    group_id BIGINT NOT NULL REFERENCES student_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (session_id, time_slot_id, group_id)
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM courses
        GROUP BY code
        HAVING COUNT(*) > 1
    ) THEN
        DELETE FROM timetable_sessions;
        DELETE FROM student_groups;

        WITH canonical_courses AS (
            SELECT id,
                   MIN(id) OVER (PARTITION BY code) AS canonical_id
            FROM courses
        )
        UPDATE course_offerings offerings
        SET course_id = canonical_courses.canonical_id
        FROM canonical_courses
        WHERE offerings.course_id = canonical_courses.id
          AND canonical_courses.id <> canonical_courses.canonical_id;

        WITH duplicate_courses AS (
            SELECT id,
                   MIN(id) OVER (PARTITION BY code) AS canonical_id
            FROM courses
        )
        DELETE FROM courses courses_to_delete
        USING duplicate_courses
        WHERE courses_to_delete.id = duplicate_courses.id
          AND duplicate_courses.id <> duplicate_courses.canonical_id;

        UPDATE course_offerings offerings
        SET total_students = term_enrollments.total_students
        FROM (
            SELECT course_id, academic_term_id, MAX(total_students) AS total_students
            FROM course_offerings
            GROUP BY course_id, academic_term_id
        ) AS term_enrollments
        WHERE offerings.course_id = term_enrollments.course_id
          AND offerings.academic_term_id = term_enrollments.academic_term_id
          AND offerings.total_students <> term_enrollments.total_students;

        INSERT INTO student_groups
            (course_id, academic_term_id, group_number, student_count)
        SELECT enrollments.course_id,
               enrollments.academic_term_id,
               group_numbers.group_number,
               LEAST(
                   20,
                   enrollments.total_students - ((group_numbers.group_number - 1) * 20)
               )
        FROM (
            SELECT course_id, academic_term_id, MAX(total_students) AS total_students
            FROM course_offerings
            GROUP BY course_id, academic_term_id
        ) AS enrollments
        CROSS JOIN LATERAL generate_series(
            1, (enrollments.total_students + 19) / 20
        ) AS group_numbers(group_number);
    END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS courses_code_key ON courses (code);