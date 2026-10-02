'use strict';

(function (root, factory) {
  const helpers = factory();
  if (typeof module === 'object' && module.exports) module.exports = helpers;
  else root.ConsoleSelection = helpers;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  function selectionPool(eligibleRows, maxAccounts) {
    if (!Array.isArray(eligibleRows) || eligibleRows.some(row => !Number.isSafeInteger(row) || row < 1)) {
      throw new Error('Choose from the current ready account rows.');
    }
    if (!Number.isSafeInteger(maxAccounts) || maxAccounts < 1) {
      throw new Error('The account limit must be a positive whole number.');
    }
    const pool = [...new Set(eligibleRows)];
    if (!pool.length) throw new Error('No ready accounts are available. Prepare accounts before selecting a test group.');
    return pool;
  }

  function selectAllRows(eligibleRows, maxAccounts) {
    const pool = selectionPool(eligibleRows, maxAccounts);
    if (pool.length > maxAccounts) {
      throw new Error(`There are ${pool.length} ready accounts, but this workspace allows ${maxAccounts} accounts per run. Select a smaller group.`);
    }
    return pool;
  }

  function selectRandomRows(eligibleRows, count, maxAccounts, random = Math.random) {
    const pool = selectionPool(eligibleRows, maxAccounts);
    const maximum = Math.min(pool.length, maxAccounts);
    if (!Number.isSafeInteger(count) || count < 1 || count > maximum) {
      throw new Error(`Choose a random selection count from 1 to ${maximum}.`);
    }
    for (let index = pool.length - 1; index > 0; index -= 1) {
      const value = random();
      if (!Number.isFinite(value) || value < 0 || value >= 1) throw new Error('The random selection could not be completed. Try selecting again.');
      const target = Math.floor(value * (index + 1));
      [pool[index], pool[target]] = [pool[target], pool[index]];
    }
    return pool.slice(0, count);
  }

  return {selectAllRows, selectRandomRows};
});
