async (page) => {
  const records = [];
  for (const [preset, expected] of [['eligible_refund','Resolution simulated'],['approval_required','Paused for operator approval'],['inventory_shortage','Resolution simulated'],['erp_unavailable','Resolution simulated'],['suspicious_request','Action blocked']]) {
    await page.getByRole('combobox', {name:'Start with a scenario'}).selectOption(preset);
    await page.getByRole('button', {name:'Run workflow →', exact:true}).click();
    await page.waitForFunction(title => !document.querySelector('#result-panel').hidden && document.querySelector('#result-title').textContent === title, expected, {timeout:15000});
    await page.getByRole('button', {name:'Run workflow →',exact:true}).waitFor({state:'visible'});
    records.push(await page.evaluate(() => ({outcome:document.querySelector('#result-title').textContent,events:document.querySelector('#event-count').textContent,status:document.querySelector('#run-status').textContent})));
    if (preset === 'approval_required') {
      await page.getByRole('button',{name:'Cancel this run',exact:true}).click();
      await page.waitForFunction(() => document.querySelector('#run-status').textContent === 'CANCELLED');
    }
  }
  await page.getByRole('tab',{name:'JSON',exact:true}).click();
  if (!(await page.locator('#inspector-content pre').textContent()).includes('SANDBOX_DETERMINISTIC')) throw new Error('Missing provenance in inspector');
  await page.getByRole('combobox',{name:'Start with a scenario'}).selectOption('eligible_refund');
  await page.getByRole('spinbutton',{name:'Requested refund',exact:true}).fill('121');
  await page.getByRole('button',{name:'Run workflow →',exact:true}).click();
  if (!(await page.locator('#form-error').textContent()).includes('cannot exceed')) throw new Error('Missing validation feedback');
  await page.getByRole('button',{name:'Reset',exact:true}).click();
  await page.locator('#simulation').screenshot({path:'output/playwright/simulator-1440.png'});
  return {scenarios:records,inspector:'PASS',amount_validation:'PASS',pending_cancellation:'PASS'};
}
