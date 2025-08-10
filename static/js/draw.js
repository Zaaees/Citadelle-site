document.addEventListener('DOMContentLoaded', () => {
  const cards = document.querySelectorAll('.draw-card');
  cards.forEach((card, index) => {
    setTimeout(() => {
      card.classList.add('revealed');
    }, index * 700);
  });
});
