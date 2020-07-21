import uuid
import pytest

@pytest.mark.flaky(reruns=2)
def test_submit_and_delete_new_assignment(
    driver, super_admin_user, public_source, red_transients_run
):
    driver.get(f"/become_user/{super_admin_user.id}")
    driver.get(f"/source/{public_source.id}")
    run_select = driver.wait_for_xpath(
        '//*[@id="mui-component-select-run_id"]'
    )
    driver.scroll_to_element_and_click(run_select)
    driver.scroll_to_element_and_click(driver.wait_for_xpath(f'//li[@data-value="{red_transients_run.id}"]'))

    comment_box = driver.wait_for_xpath("//textarea[@name='comment']")
    comment_text = str(uuid.uuid4())
    comment_box.send_keys(comment_text)

    submit_button = driver.wait_for_xpath(
        '//*[@name="assignmentSubmitButton"]'
    )

    driver.scroll_to_element_and_click(submit_button)

    delbut = driver.wait_for_xpath('//button[text()="Delete"]')
    driver.scroll_to_element_and_click(delbut)
    driver.wait_for_xpath_to_disappear('//button[text()="Delete"]')

