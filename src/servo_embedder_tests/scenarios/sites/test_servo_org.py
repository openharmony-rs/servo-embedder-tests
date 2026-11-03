# Copyright 2025 The Servo Project Developers. See the COPYRIGHT
# file at the top-level directory of this distribution.
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or http://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

"""servo.org, ported from Servo's etc/ci/scenario. In CI it runs against a replay (`--proxy replay`)."""

import pytest
from selenium.webdriver.common.by import By

pytestmark = pytest.mark.network


def test_servo_org(servo):
    driver = servo.driver
    driver.get("https://servo.org")
    driver.implicitly_wait(6)
    driver.find_element(By.CSS_SELECTOR, "#homeHero > div.hero-body > div.container > div > a:nth-child(1)")
    driver.find_element(By.CSS_SELECTOR, "#homeHero > div.hero-body > div.container > h1")
