# Copyright 2025 The Servo Project Developers. See the COPYRIGHT
# file at the top-level directory of this distribution.
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or http://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

"""Helpers for the Mossel shopping site, ported from Servo's etc/ci/scenario."""

import time
from urllib.parse import urlsplit

from selenium.common import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver

URL = "https://m.huaweimossel.com"
MOBILE_HOST = "m.huaweimossel.com"
POPUP_BUTTON = (
    "#app > uni-app > uni-page > uni-page-wrapper > uni-page-body > uni-view "
    "> uni-view:nth-child(5) "
    "> uni-view.m-popup.m-popup_transition.m-mask_show.m-mask_fade.m-popup_push.m-fixed_mid "
    "> uni-view > uni-view > uni-button:nth-child(1)"
)
HOME_ITEM = (
    "#app > uni-app > uni-page > uni-page-wrapper > uni-page-body > uni-view "
    "> uni-view.idx-swiper.m-bgWhite.m-main > uni-scroll-view:nth-child(1) > div > div > div "
    "> uni-view:nth-child(3)"
)
CATEGORY_TAB = "div.uni-tabbar__item:nth-child(3)"
CATEGORY_CONTENT = "#goodsGroup"


def load(driver: WebDriver, attempts: int = 5) -> None:
    """Loads the home page, reloading while the site's own load-timeout error page shows."""
    driver.set_page_load_timeout(30)
    for _ in range(attempts):
        try:
            driver.get(URL)
        except Exception:
            continue
        try:
            driver.find_element(By.CSS_SELECTOR, ".uni-async-error")
        except NoSuchElementException:
            check_mobile_site(driver)
            return
    raise AssertionError(f"Mossel did not load in {attempts} attempts")


def check_mobile_site(driver: WebDriver) -> None:
    """Fails if the site sent the browser to its desktop site, which the scenarios don't cover."""
    if urlsplit(driver.current_url).hostname == MOBILE_HOST:
        return
    width = driver.execute_script("return screen.width")
    raise AssertionError(
        f"Mossel redirected to its desktop site {driver.current_url}: it does that unless the user agent is a "
        f"phone's and screen.width is below 550, and here screen.width is {width}"
    )


def close_popup(driver: WebDriver) -> None:
    """Closes the promotion pop-up if there is one; it depends on the season and on local storage."""
    try:
        driver.find_element(By.CSS_SELECTOR, POPUP_BUTTON).click()
        time.sleep(1)
    except NoSuchElementException:
        pass


def open_categories(driver: WebDriver) -> None:
    driver.find_element(By.CSS_SELECTOR, CATEGORY_TAB).click()


def wait_for_categories(driver: WebDriver, attempts: int = 5) -> None:
    driver.implicitly_wait(30)
    for _ in range(attempts):
        try:
            driver.find_element(By.CSS_SELECTOR, CATEGORY_CONTENT)
            return
        except NoSuchElementException:
            try:
                driver.refresh()
            except Exception:
                pass
    raise AssertionError("Mossel's category page did not show its content")
