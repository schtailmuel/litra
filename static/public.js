const publicImageModal = document.querySelector("#publicImageModal");
const publicModalImage = document.querySelector("#publicModalImage");
const publicModalClose = document.querySelector(".public-modal-close");

function closePublicImageModal() {
  if (!publicImageModal) {
    return;
  }
  if (publicImageModal.close) {
    publicImageModal.close();
  } else {
    publicImageModal.classList.remove("open");
  }
  if (publicModalImage) {
    publicModalImage.removeAttribute("src");
  }
}

document.querySelectorAll("[data-modal-image]").forEach((button) => {
  button.addEventListener("click", () => {
    if (!publicImageModal || !publicModalImage) {
      return;
    }
    publicModalImage.src = button.dataset.modalImage;
    publicModalImage.alt = button.dataset.modalAlt || "";
    if (publicImageModal.showModal) {
      publicImageModal.showModal();
    } else {
      publicImageModal.classList.add("open");
    }
  });
});

if (publicModalClose) {
  publicModalClose.addEventListener("click", closePublicImageModal);
}

if (publicImageModal) {
  publicImageModal.addEventListener("click", (event) => {
    if (event.target === publicImageModal) {
      closePublicImageModal();
    }
  });
}
