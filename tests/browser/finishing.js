async (page) => {
  const ui = {};
  await page.setViewportSize({width:375,height:812});
  ui.closed_drawer_inert = await page.locator('#sidebar').evaluate(el=>el.inert);
  if (!ui.closed_drawer_inert) throw new Error('Closed mobile navigation is focusable');
  await page.getByRole('button',{name:'Menu',exact:true}).click();
  await page.waitForFunction(() => Math.abs(document.querySelector('#sidebar').getBoundingClientRect().left)<1);
  await page.screenshot({path:'output/playwright/mobile-drawer.png'});
  await page.keyboard.press('Escape');
  ui.drawer_focus_return = await page.getByRole('button',{name:'Menu',exact:true}).evaluate(el=>el===document.activeElement);
  await page.getByRole('combobox',{name:'Start with a scenario'}).selectOption('eligible_refund');
  await page.getByRole('button',{name:'Run workflow →',exact:true}).click();
  await page.waitForFunction(() => document.querySelector('#result-title').textContent==='Resolution simulated' && !document.querySelector('#result-panel').hidden);
  await page.getByRole('spinbutton',{name:'Requested refund',exact:true}).fill('81');
  ui.changed_input_notice = await page.locator('#input-status').isVisible();
  if (!ui.changed_input_notice) throw new Error('Missing changed-input notice');
  await page.getByRole('button',{name:'Reset',exact:true}).click();
  await page.getByRole('tab',{name:'Technical',exact:true}).click();
  await page.locator('#simulation').screenshot({path:'output/playwright/simulator-375.png'});
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('#simulation').screenshot({path:'output/playwright/simulator-1440.png'});
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:'output/playwright/desktop-1440.png'});
  return ui;
}
