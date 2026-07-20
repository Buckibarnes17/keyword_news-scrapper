export function getNextScheduledDateLocal(frequency, timeStr, weekdayVal, dayVal) {
  const [hours, minutes] = timeStr.split(':').map(Number);
  const now = new Date();
  
  // Construct candidate Date object in local timezone
  let candidate = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hours, minutes, 0, 0);
  
  if (frequency === 'daily') {
    if (candidate <= now) {
      candidate.setDate(candidate.getDate() + 1);
    }
  } else if (frequency === 'weekly') {
    const targetWeekday = Number(weekdayVal);
    const currentWeekday = now.getDay();
    let daysToAdd = (targetWeekday - currentWeekday + 7) % 7;
    if (daysToAdd === 0 && candidate <= now) {
      daysToAdd = 7;
    }
    candidate.setDate(candidate.getDate() + daysToAdd);
  } else if (frequency === 'monthly') {
    const targetDay = Number(dayVal);
    candidate.setDate(targetDay);
    if (candidate.getDate() !== targetDay || candidate <= now) {
      let nextMonth = candidate.getMonth() + 1;
      let nextYear = candidate.getFullYear();
      if (nextMonth > 11) {
        nextMonth = 0;
        nextYear++;
      }
      const maxDays = new Date(nextYear, nextMonth + 1, 0).getDate();
      const actualDay = Math.min(targetDay, maxDays);
      candidate = new Date(nextYear, nextMonth, actualDay, hours, minutes, 0, 0);
    }
  }
  
  return candidate;
}
