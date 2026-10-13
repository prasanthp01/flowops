async (page) => {
  const sizes=[];
  for (const width of [320,375,430,768,1280,1440]) {
    await page.setViewportSize({width,height:950});
    await page.evaluate(() => window.scrollTo(0,0));
    await page.screenshot({path:`output/playwright/viewport-${width}.png`,fullPage:false});
    const dimensions=await page.evaluate(() => ({width:innerWidth,scroll:document.documentElement.scrollWidth,main:document.querySelector('main').getBoundingClientRect().width}));
    if (dimensions.scroll>width) throw new Error(`Horizontal page overflow at ${width}: ${dimensions.scroll}`);
    sizes.push(dimensions);
  }
  await page.setViewportSize({width:375,height:812});
  await page.getByRole('button',{name:'Menu',exact:true}).click();
  if (!(await page.locator('#main').evaluate(el=>el.inert))) throw new Error('Drawer background not inert');
  await page.screenshot({path:'output/playwright/mobile-drawer.png'});
  await page.keyboard.press('Escape');
  if (!(await page.getByRole('button',{name:'Menu',exact:true}).evaluate(el=>el===document.activeElement))) throw new Error('Focus was not returned');
  await page.locator('#simulation').screenshot({path:'output/playwright/simulator-375.png'});
  await page.getByRole('tab',{name:'Overview',exact:true}).click();
  await page.keyboard.press('ArrowRight');
  if (await page.getByRole('tab',{name:'Technical',exact:true}).getAttribute('aria-selected') !== 'true') throw new Error('Inspector arrow-key navigation failed');
  return {viewports:sizes,mobile_drawer_escape_focus_return:'PASS',keyboard_tabs:'PASS'};
}
