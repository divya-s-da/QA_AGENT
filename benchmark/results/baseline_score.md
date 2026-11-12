# ShopLite benchmark score

- Planted bugs: 22
- Caught: 1 (5%); T0 catch rate 0%
- False passes: 0 (of them on success-trap bugs: 0)
- Missed on a tested route: 11; route never reached: 10
- Verdicts: 67 {'BLOCKED': 60, 'FAIL': 2, 'PASS': 5}
- Routes reached: /, /asdfghjkl, /cart, /login, /register
- FAIL verdicts not linked to a planted bug: 0
- Clean build: 6 FAIL of 69 verdicts (false-fail rate 9%)

Linking is by keyword and is approximate. Confirm each linked line.

| Bug | Tier | Trap | Route | Result | Summary |
|---|---|---|---|---|---|
| B01 | T0 | yes | /cart | **missed** | Place order shows 'Order placed!' but the API returns 500 and no order is saved |
| B02 | T0 |  | /cart | **missed** | Cart total ignores quantity (2 x $20 shows $20) |
| B03 | T0 |  | /admin | **not_reached** | A regular user can open /admin and see every user's email |
| B04 | T0 |  | /orders | **not_reached** | Cancel in the 'Delete this order?' dialog deletes the order anyway |
| B05 | T0 | yes | /account | **not_reached** | Log out goes to the login page but the server session stays valid |
| B06 | T1 |  | / | **missed** | Search returns every product for any query |
| B07 | T1 | yes | /register | **caught** | Register accepts an invalid email ('abc') and shows success |
| B08 | T1 | yes | /account | **not_reached** | Profile Save shows 'Saved' but the name reverts after reload |
| B09 | T1 |  | / | **missed** | Page 2 of the product list shows the same items as page 1 |
| B10 | T1 |  | / | **missed** | Sort by price sorts as text ($100.00 comes before $12.00) |
| B11 | T1 |  | /product | **not_reached** | Double-clicking Post review creates two identical reviews |
| B12 | T1 |  | /product | **not_reached** | The Reviews tab does nothing when clicked |
| B13 | T1 |  | /cart | **missed** | 'Continue shopping' links to /shop, which is a 404 |
| B14 | T1 | yes | /cart | **missed** | Coupon SAVE10 shows 'Coupon applied' but the total does not change |
| B15 | T2 |  | / | **missed** | Desk Lamp product image is broken (404) |
| B16 | T2 |  | / | **missed** | Newsletter email input has no label |
| B17 | T2 |  | /orders | **not_reached** | Orders page scrolls horizontally (a 1600px wide element) |
| B18 | T2 |  | /about | **not_reached** | About page skips heading levels (h1 then h4) |
| B19 | T2 |  | /contact | **not_reached** | Contact page has an empty <title> |
| B20 | T2 |  | / | **missed** | Shop page throws an uncaught JavaScript error on load |
| B21 | T2 |  | / | **missed** | Footer 'Help' link has an empty href |
| B22 | T1 |  | /product | **not_reached** | /product/999 shows a stack trace and a database URL (500) instead of a 404 page |

## Linked verdicts

**B07 (caught)**
- `invariant_no_empty_hrefs` FAIL on /register: [url: http://127.0.0.1:8791/register] [APP_CODE] check_page_invariants reports 'no_empty_hrefs: false - 1 link with empty href'. The footer 'Help' link (ref f7e27) has href="" on the /register page. This is a link with a
- `screen_2_field_type_validation` FAIL on /register: [url: http://127.0.0.1:8791/register] [APP_CODE] Typed Name='Test User', Email='abc' (invalid, not an email address), Password='password123' and clicked Create account. Instead of a type-specific email validation error, 

**B09 (missed)**
- `screen_0_page_has_title` BLOCKED on /: No evidence recorded by navigator

**B13 (missed)**
- `screen_3_all_links_navigable` BLOCKED on /cart: No evidence recorded by navigator
- `screen_3_deep_link_access` BLOCKED on /cart: No evidence recorded by navigator

**B15 (missed)**
- `screen_0_images_have_alt` BLOCKED on /: No evidence recorded by navigator

**B20 (missed)**
- `screen_0_error_boundary` BLOCKED on /: No evidence recorded by navigator
- `screen_0_network_error_display` BLOCKED on /: No evidence recorded by navigator
- `screen_0_form_submission_error` BLOCKED on /: No evidence recorded by navigator


## FAIL verdicts on the clean build

- `screen_0_required_field_validation` on /: [url: http://127.0.0.1:8791/] On the home page, the only data-entry form is the newsletter subscribe (email field) plus the search form. Clicked the Subscribe button with an EMPTY email field. No validation error appeare
- `screen_0_field_type_validation` on /: [url: http://127.0.0.1:8791/] In the newsletter email field I typed 'abc' (invalid email format) and clicked Subscribe. The page displayed "Thanks for subscribing" with NO type-validation error. The field does not enforc
- `screen_1_required_field_validation` on /login: [url: http://127.0.0.1:8791/login] Clicked the "Log in" submit button with both the Email and Password fields empty. The page did NOT display any validation error messages below the required fields. Page text remained id
- `screen_1_field_type_validation` on /login: [url: http://127.0.0.1:8791/login] Typed 'abc' into the Email (email-type) field and clicked "Log in". No type-specific validation error appeared; page text unchanged and no network request fired. The email input evident
- `screen_2_required_field_validation` on /register: [url: http://127.0.0.1:8791/register] Clicked 'Create account' with all three fields (Name, Email, Password) empty. No error messages appeared below any field. Page state unchanged (same labels, no aria-invalid, no error
- `screen_2_field_type_validation` on /register: [url: http://127.0.0.1:8791/register] Filled Name='John', Email='abc' (invalid email format), Password='123' (<8 chars) and submitted. No type-specific validation errors appeared in the DOM and no network request fired. 
