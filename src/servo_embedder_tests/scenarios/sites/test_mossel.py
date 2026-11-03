# Copyright 2025 The Servo Project Developers. See the COPYRIGHT
# file at the top-level directory of this distribution.
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or http://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

"""A mobile shopping site, ported from Servo's etc/ci/scenario. In CI it runs against a replay (`--proxy replay`)."""

import time

import pytest
from selenium.webdriver.common.by import By

from servo_embedder_tests.oracles import wait_until

from . import mossel

# Servo's CI has only ever run it on HarmonyOS.
pytestmark = [pytest.mark.network, pytest.mark.platform("ohos")]


def test_mossel_home(servo):
    driver = servo.driver
    driver.implicitly_wait(6)
    mossel.load(driver)
    mossel.close_popup(driver)
    driver.find_element(By.CSS_SELECTOR, mossel.HOME_ITEM)


def test_mossel_redirect(servo):
    driver = servo.driver
    driver.implicitly_wait(6)
    mossel.load(driver)
    mossel.close_popup(driver)
    time.sleep(2)
    mossel.open_categories(driver)
    mossel.wait_for_categories(driver)


@pytest.mark.servo_prefs(session_history_max_length=0)
def test_mossel_slide(servo, device):
    driver = servo.driver
    driver.implicitly_wait(6)
    mossel.load(driver)
    mossel.close_popup(driver)
    mossel.open_categories(driver)
    mossel.wait_for_categories(driver)
    before = driver.get_screenshot_as_base64()
    webview = device.dump_layout().webview()
    assert webview is not None, "no XComponent in the layout"
    left, top, right, bottom = webview.bounds
    x = (left + right) // 2
    device.swipe(x, top + (bottom - top) * 3 // 4, x, top + (bottom - top) // 4, 500)
    wait_until(lambda: driver.get_screenshot_as_base64() != before, timeout=10, message="the page to scroll")
