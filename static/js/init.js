// The file list is instant; reading every rating means one exiftool pass
// over the whole shoot and took 7.5s on a 665-frame library. Show the
// library first and paint the ratings in when they arrive.
loadFiles();
loadRatings().then(() => {
  syncGridState();
  paintRating();
  if (filterMode !== "all") applyFilter();
});
loadPresets();
