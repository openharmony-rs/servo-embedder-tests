"""Load local pages and move through session history."""

from selenium.webdriver.common.by import By

from servo_embedder_tests.oracles import wait_until


def test_loads_local_page(servo, pages):
    driver = servo.driver
    driver.get(pages.url("basic.html"))
    assert driver.title == "servo-embedder-tests basic"
    wait_until(lambda: driver.find_element(By.ID, "status").text == "loaded", message="load event")


def test_history_back_and_forward(servo, pages):
    driver = servo.driver
    driver.get(pages.url("history-a.html"))
    driver.find_element(By.ID, "next").click()
    wait_until(lambda: driver.current_url.endswith("history-b.html"), message="navigation to B")
    driver.back()
    wait_until(lambda: driver.find_element(By.ID, "page").text == "A", message="back to A")
    driver.forward()
    wait_until(lambda: driver.find_element(By.ID, "page").text == "B", message="forward to B")
