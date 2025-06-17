(function () {
  "use strict";

  function makeApiCall(url, options = {}) {
    const defaultOptions = {
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
    };

    const finalOptions = Object.assign({}, defaultOptions, options);

    const fetchFn =
      typeof CTFd !== "undefined" && CTFd.fetch ? CTFd.fetch : fetch;

    return fetchFn(url, finalOptions).then((response) => {
      if (typeof response.json === "function") {
        return response.json();
      }
      return response;
    });
  }

  function showAlert(title, message, type = "info") {
    console.log(`${title}: ${message}`);

    if (
      typeof CTFd !== "undefined" &&
      CTFd.ui &&
      CTFd.ui.ezq &&
      CTFd.ui.ezq.ezAlert
    ) {
      CTFd.ui.ezq.ezAlert({
        title: title,
        body: message,
        button: "OK",
      });
    } else if (typeof alert === "function") {
      alert(`${title}: ${message}`);
    } else {
      console.warn("No alert method available");
    }
  }

  window.WhaleImageManager = {
    loadAvailableImages: function () {
      const dropdown = document.getElementById("available-images");
      const loadButton = document.getElementById("load-images-dropdown");

      if (!dropdown || !loadButton) {
        console.error("Image dropdown elements not found");
        return;
      }

      dropdown.innerHTML =
        '<option value="">Loading available images...</option>';
      dropdown.style.display = "block";
      loadButton.disabled = true;

      makeApiCall("/api/v1/plugins/ctfd-whale/admin/images/list", {
        method: "GET",
      })
        .then((data) => {
          console.log("Images API response:", data);

          if (
            data.success &&
            data.data &&
            data.data.images &&
            data.data.images.length > 0
          ) {
            dropdown.innerHTML = '<option value="">Select an image...</option>';
            data.data.images.forEach((image) => {
              const option = document.createElement("option");
              option.value = image;
              option.textContent = image;
              dropdown.appendChild(option);
            });
          } else {
            const message =
              data.message || "No images found with configured prefix";
            dropdown.innerHTML = `<option value="">${message}</option>`;

            if (!data.success) {
              showAlert("Error", message, "error");
            }
          }
        })
        .catch((error) => {
          console.error("Error loading images:", error);
          dropdown.innerHTML = '<option value="">Error loading images</option>';
          showAlert(
            "Error",
            "Failed to load images: " + error.message,
            "error"
          );
        })
        .finally(() => {
          loadButton.disabled = false;
        });
    },

    refreshImagesList: function () {
      const refreshButton = document.getElementById("refresh-images-list");

      if (!refreshButton) {
        console.error("Refresh button not found");
        return;
      }

      const originalText = refreshButton.innerHTML;

      refreshButton.disabled = true;
      refreshButton.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

      makeApiCall("/api/v1/plugins/ctfd-whale/admin/images/refresh", {
        method: "POST",
      })
        .then((data) => {
          console.log("Refresh API response:", data);

          if (data.success) {
            this.loadAvailableImages();
            showAlert("Success", data.message, "success");
          } else {
            const errorMsg = data.message || "Failed to refresh images";
            showAlert("Error", errorMsg, "error");
          }
        })
        .catch((error) => {
          console.error("Error refreshing images:", error);
          showAlert(
            "Error",
            "Failed to refresh images: " + error.message,
            "error"
          );
        })
        .finally(() => {
          refreshButton.disabled = false;
          refreshButton.innerHTML = originalText;
        });
    },

    setupImageDropdown: function () {
      console.log("Setting up image dropdown functionality");

      const loadButton = document.getElementById("load-images-dropdown");
      if (loadButton) {
        loadButton.addEventListener("click", (e) => {
          e.preventDefault();
          console.log("Load images clicked");
          this.loadAvailableImages();
        });
      } else {
        console.warn("Load images button not found");
      }

      const refreshButton = document.getElementById("refresh-images-list");
      if (refreshButton) {
        refreshButton.addEventListener("click", (e) => {
          e.preventDefault();
          console.log("Refresh images clicked");
          this.refreshImagesList();
        });
      } else {
        console.warn("Refresh images button not found");
      }

      const dropdown = document.getElementById("available-images");
      if (dropdown) {
        dropdown.addEventListener("change", () => {
          const selectedImage = dropdown.value;
          console.log("Image selected:", selectedImage);
          if (selectedImage) {
            const dockerImageInput = document.getElementById("docker_image");
            if (dockerImageInput) {
              dockerImageInput.value = selectedImage;
              dropdown.style.display = "none";
            }
          }
        });
      } else {
        console.warn("Available images dropdown not found");
      }

      const urlParams = new URLSearchParams(window.location.search);
      const preselectedImage = urlParams.get("image");
      if (preselectedImage) {
        const dockerImageInput = document.getElementById("docker_image");
        if (dockerImageInput) {
          dockerImageInput.value = preselectedImage;
          console.log("Pre-selected image from URL:", preselectedImage);
        }
      }
    },

    init: function () {
      console.log("Initializing Whale Image Manager");

      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => {
          this.setupImageDropdown();
        });
      } else {
        this.setupImageDropdown();
      }
    },
  };

  window.WhaleImageManager.init();
})();
