// Reveal each drawn card with a slight delay to create a cascade animation.
document.addEventListener('DOMContentLoaded', () => {
  const cards = document.querySelectorAll('.draw-card');
  cards.forEach((card, index) => {
    setTimeout(() => {
      card.classList.add('revealed');
    }, index * 700);
  });
});