# Olist fixture CSVs (synthetic)

Hand-made, **synthetic** rows with the published Olist headers (DATA_CONTRACT §2), used by the
loader tests. Not real data. Edge cases on purpose: a review whose comment spans several lines and
contains escaped quotes (`order_reviews`, CRLF line endings), unquoted empty fields (load as NULL),
a quoted empty string (loads as ''), and a UTF-8 byte-order mark in `product_category_name_translation.csv`.
