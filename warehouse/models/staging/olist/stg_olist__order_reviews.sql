-- Grain: one row per (review_id, order_id) = one row per raw_olist.order_reviews record.
-- review_id alone is NOT unique (the same review can be attached to several orders) and an order
-- can have several reviews. Typed and cleaned only: empty strings -> NULL (comment title and
-- message are often empty), score -> integer, dates -> timestamp. Messages keep their line breaks.

with source as (

    select * from {{ source('olist', 'order_reviews') }}

),

cleaned as (

    select
        nullif(review_id, '') as review_id,
        nullif(order_id, '') as order_id,
        nullif(review_score, '') as review_score,
        nullif(review_comment_title, '') as review_comment_title,
        nullif(review_comment_message, '') as review_comment_message,
        nullif(review_creation_date, '') as review_creation_date,
        nullif(review_answer_timestamp, '') as review_answer_timestamp
    from source

),

typed as (

    select
        review_id,
        order_id,
        review_score::integer as review_score,
        review_comment_title,
        review_comment_message,
        review_creation_date::timestamp as review_creation_date,
        review_answer_timestamp::timestamp as review_answer_timestamp
    from cleaned

)

select * from typed
